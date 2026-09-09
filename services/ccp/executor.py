"""Job execution: parallel shell over ClusterShell, Ansible playbooks, and the
node-lifecycle jobs (onboarding / verification).

Jobs run in a background daemon thread and stream their output to a per-job log
file under JOBS_DIR; the jobs table (status/exit_code) is the source of truth so
the UI can poll regardless of which worker/thread produced the job. Nodes marked
conn='local' run directly via subprocess so the panel is demoable without any
reachable SSH hosts; conn='ssh' nodes are executed in parallel through
ClusterShell (shell jobs) or ansible-playbook (playbook jobs).

Lifecycle: an ssh node is only ever set to state='managed' by the onboard/verify
job in this module, after (1) password auth succeeds, (2) the CCP public key is
installed, (3) a command runs over key auth. Passwords are handed to start_job
via the `secret` argument, kept in a process-local dict, and consumed by the job
thread — they are never written to jobs.spec, the database, or job logs.
"""
import os
import re
import sys
import json
import time
import shlex
import tempfile
import threading
import subprocess

import db
import topology

JOBS_DIR = os.environ.get('CCP_JOBS_DIR', '/data/ccp/jobs')
SSH_KEY = os.environ.get('CCP_SSH_KEY', '/data/ccp/ssh/id_ccp')
# hard wall-clock cap per job so a hung command/playbook can't pin a worker
# thread (and leave the job stuck 'running') forever
JOB_TIMEOUT = int(os.environ.get('CCP_JOB_TIMEOUT', '900'))
SSH_COMMON = ['-o', 'StrictHostKeyChecking=no',
              '-o', 'UserKnownHostsFile=/dev/null',
              '-o', 'ConnectTimeout=10',
              '-o', 'BatchMode=yes',
              # keep "Warning: Permanently added … to the list of known hosts"
              # out of every job log — it is noise, not output
              '-o', 'LogLevel=ERROR']
# per-step wall clock for onboarding ssh commands (connect timeout is separate)
ONBOARD_STEP_TIMEOUT = int(os.environ.get('CCP_ONBOARD_STEP_TIMEOUT', '60'))


def ensure_ssh_key():
    """Generate the CCP ed25519 keypair on first start. The onboarding job
    installs the public half into each node's authorized_keys; all regular
    execution then runs over key auth."""
    if os.path.exists(SSH_KEY):
        return
    try:
        os.makedirs(os.path.dirname(SSH_KEY), exist_ok=True)
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '',
                        '-C', 'ccp-control-panel', '-f', SSH_KEY],
                       check=True, capture_output=True)
        os.chmod(SSH_KEY, 0o600)
    except Exception as exc:
        # Not fatal at import time: onboarding will fail loudly with a clear
        # message; shell/ansible against already-keyed nodes still work.
        sys.stderr.write(f'[ccp] WARNING: could not generate SSH key {SSH_KEY}: {exc}\n')


def public_key():
    try:
        with open(SSH_KEY + '.pub') as fh:
            return fh.read().strip()
    except OSError:
        return ''


def _log_path(job_id):
    os.makedirs(JOBS_DIR, exist_ok=True)
    return os.path.join(JOBS_DIR, f'{job_id}.log')


def job_log(job_id):
    try:
        with open(_log_path(job_id), 'r', errors='replace') as fh:
            return fh.read()
    except FileNotFoundError:
        return ''


def _finish(job_id, status, exit_code):
    db.execute('UPDATE jobs SET status=?, exit_code=?, finished_at=? WHERE id=?',
               (status, exit_code, int(time.time()), job_id))


# Secrets (onboarding passwords) ride alongside a job in process memory only —
# spec is persisted to the jobs table, secrets never are. The job thread pops
# its secret on start; anything left over (thread never started) is dropped
# when the process exits.
_secrets = {}
_secrets_lock = threading.Lock()


def start_job(kind, target, spec, created_by, secret=None):
    """Insert a job row and kick off its background thread. Returns the job id.

    `secret` (e.g. an onboarding password) is kept in memory for the job
    thread and is never serialized into the persisted spec."""
    job_id = db.execute(
        'INSERT INTO jobs (kind, target, spec, status, created_by, created_at) '
        'VALUES (?,?,?,?,?,?)',
        (kind, target, json.dumps(spec), 'running', created_by, int(time.time())))
    open(_log_path(job_id), 'w').close()
    if secret is not None:
        with _secrets_lock:
            _secrets[job_id] = secret
    if os.environ.get('CCP_TEST_SYNC_JOBS') == '1':
        _run(job_id, kind, spec)        # tests: run inline, deterministic
        return job_id
    t = threading.Thread(target=_run, args=(job_id, kind, spec), daemon=True)
    t.start()
    return job_id


def _run(job_id, kind, spec):
    with _secrets_lock:
        secret = _secrets.pop(job_id, None)
    log = open(_log_path(job_id), 'a', buffering=1)
    try:
        if kind == 'shell':
            rc = _run_shell(job_id, spec, log)
        elif kind in ('ansible', 'slurm_deploy'):
            rc = _run_ansible(job_id, spec, log)
        elif kind in ('onboard', 'verify'):
            rc = _run_onboard(job_id, spec, secret, log)
        elif kind == 'hwscan':
            rc = _run_hwscan(job_id, spec, log)
        elif kind == 'hostname':
            rc = _run_hostname(job_id, spec, log)
        elif kind == 'slurm_action':
            rc = _run_slurm_action(job_id, spec, log)
        elif kind == 'filedeploy':
            rc = _run_filedeploy(job_id, spec, log)
        else:
            log.write(f'unknown job kind: {kind}\n')
            rc = 2
        # slurm lifecycle jobs advance the cluster's stage on success
        if rc == 0 and spec.get('advance_to') and spec.get('cluster_id'):
            db.execute('UPDATE clusters SET slurm_state=? WHERE id=?',
                       (spec['advance_to'], spec['cluster_id']))
            log.write(f'\n[ccp] cluster lifecycle → {spec["advance_to"]}\n')
        _finish(job_id, 'success' if rc == 0 else 'failed', rc)
    except Exception as exc:  # never let a job thread die silently
        log.write(f'\n[ccp] job crashed: {exc}\n')
        _finish(job_id, 'failed', 1)
    finally:
        del secret
        log.close()


# ── shell / ClusterShell ─────────────────────────────────────────────────────

def _run_shell(job_id, spec, log):
    nodes = _resolve_nodes(spec.get('node_ids', []), log)
    command = spec.get('command', '')
    if not nodes:
        log.write('[ccp] no target nodes resolved\n')
        return 2

    worst = 0
    local = [n for n in nodes if n['conn'] == 'local']
    remote = [n for n in nodes if n['conn'] != 'local']

    for n in local:
        try:
            p = subprocess.run(['/bin/sh', '-c', command],
                               capture_output=True, text=True, timeout=JOB_TIMEOUT)
            rc, out = p.returncode, (p.stdout or '') + (p.stderr or '')
        except subprocess.TimeoutExpired:
            rc = 124
            out = f'[ccp] command timed out after {JOB_TIMEOUT}s\n'
        status = 'SUCCESS' if rc == 0 else 'FAILED'
        log.write(f'===== {n["name"]} ({n["address"]}, local) | '
                  f'STATUS: {status} =====\n{out.rstrip(chr(10))}\n'
                  f'[{n["name"]} exit {rc}]\n\n')
        worst = max(worst, rc)

    if remote:
        worst = max(worst, _run_shell_clustershell(remote, command, log))
    return worst


def _run_shell_clustershell(remote, command, log):
    """Run one ClusterShell Task per distinct (user, port) bucket so nodes with
    different SSH settings still execute in parallel within their bucket."""
    try:
        from ClusterShell.Task import task_self
        from ClusterShell.NodeSet import NodeSet
    except Exception as exc:
        log.write(f'[ccp] ClusterShell unavailable: {exc}\n')
        return 1

    buckets = {}
    addr2name = {}
    for n in remote:
        buckets.setdefault((n['ssh_user'], n['ssh_port']), []).append(n['address'])
        addr2name[n['address']] = n['name']

    worst = 0
    for (user, port), addrs in buckets.items():
        task = task_self()
        ssh_opts = list(SSH_COMMON) + ['-p', str(port)]
        if os.path.exists(SSH_KEY):
            ssh_opts += ['-i', SSH_KEY]
        task.set_info('ssh_user', user)
        task.set_info('ssh_options', ' '.join(shlex.quote(o) for o in ssh_opts))
        task.run(command, nodes=NodeSet.fromlist(addrs), timeout=JOB_TIMEOUT)

        # ClusterShell hands back all output buffers first and all return codes
        # afterwards. Collate them per host BEFORE writing anything, so each
        # host's block owns its own output and its own exit line — emitting as
        # they arrive leaked every trailing '[host exit N]' into whichever
        # block happened to be last.
        collected = {}          # address -> {'out': str, 'rc': int|None}

        def slot(addr):
            return collected.setdefault(str(addr), {'out': '', 'rc': None})

        for addr in addrs:
            slot(addr)
        for buf, nodelist in task.iter_buffers():
            text = (buf.message().decode('utf-8', 'replace')
                    if hasattr(buf, 'message') else str(buf))
            for node in nodelist:
                slot(node)['out'] += text
        for rc, nodelist in task.iter_retcodes():
            for node in nodelist:
                slot(node)['rc'] = rc
        for node in task.iter_keys_timeout():
            s = slot(node)
            s['rc'] = 124
            s['out'] += f'[ccp] timed out after {JOB_TIMEOUT}s\n'

        for addr in addrs:
            s = collected[str(addr)]
            rc = 124 if s['rc'] is None and task.num_timeout() else (s['rc'] or 0)
            name = addr2name.get(str(addr), str(addr))
            status = 'SUCCESS' if rc == 0 else 'FAILED'
            log.write(f'===== {name} ({addr}, ssh {user}@:{port}) | '
                      f'STATUS: {status} =====\n')
            log.write(s['out'].rstrip('\n') + '\n')
            log.write(f'[{name} exit {rc}]\n\n')
            worst = max(worst, rc)
    return worst


# ── node onboarding / verification ───────────────────────────────────────────

def _password_ssh(address, user, port, command, password):
    """One command over SSH with password auth (sshpass). Returns (rc, output).
    rc -1 = timeout / local failure; rc 5 = sshpass 'wrong password'."""
    cmd = ['sshpass', '-e', 'ssh',
           '-o', 'StrictHostKeyChecking=no',
           '-o', 'UserKnownHostsFile=/dev/null',
           '-o', 'ConnectTimeout=10',
           '-o', 'PreferredAuthentications=password,keyboard-interactive',
           '-o', 'PubkeyAuthentication=no',
           '-o', 'NumberOfPasswordPrompts=1',
           '-o', 'LogLevel=ERROR',
           '-p', str(port), f'{user}@{address}', command]
    env = dict(os.environ, SSHPASS=password)   # env, not argv: /proc-safe
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=ONBOARD_STEP_TIMEOUT, env=env)
        return p.returncode, (p.stdout or '') + (p.stderr or '')
    except subprocess.TimeoutExpired:
        return -1, f'timed out after {ONBOARD_STEP_TIMEOUT}s'
    except FileNotFoundError as exc:
        return -1, f'sshpass/ssh not installed in the CCP container: {exc}'


def _key_ssh(address, user, port, command, timeout=None):
    """One command over SSH with the CCP key (BatchMode). Returns (rc, output).
    `timeout` overrides the per-step default for long-running remote work
    (e.g. waiting on a batch job)."""
    cmd = (['ssh'] + list(SSH_COMMON) + ['-i', SSH_KEY, '-p', str(port),
           f'{user}@{address}', command])
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout or ONBOARD_STEP_TIMEOUT)
        return p.returncode, (p.stdout or '') + (p.stderr or '')
    except subprocess.TimeoutExpired:
        return -1, f'timed out after {timeout or ONBOARD_STEP_TIMEOUT}s'
    except FileNotFoundError as exc:
        return -1, f'ssh not installed in the CCP container: {exc}'


def _set_node_state(node_id, state, detail=''):
    db.execute('UPDATE nodes SET state=?, state_detail=? WHERE id=?',
               (state, detail[:500], node_id))


def _auth_failure_reason(rc, out):
    """Map a failed password-ssh attempt to an operator-readable reason."""
    if rc == 5 or 'Permission denied' in out:
        return 'auth failed: wrong username or password'
    if rc == -1 or 'timed out' in out or 'Connection timed out' in out:
        return 'unreachable: connection timed out'
    if 'Connection refused' in out:
        return 'unreachable: connection refused (is sshd running?)'
    if 'Could not resolve' in out or 'Name or service not known' in out:
        return 'unreachable: hostname does not resolve'
    return f'ssh failed (exit {rc}): {out.strip().splitlines()[-1][:200] if out.strip() else "no output"}'


def _run_onboard(job_id, spec, secret, log):
    """Lifecycle job for one ssh node.

    mode 'onboard' (kind onboard, secret = password):
      [1/3] password auth works  [2/3] install CCP key  [3/3] command over key
    mode 'verify' (kind verify, no secret): step 3 only — for legacy rows that
    already carry a working key.

    Success ⇒ node state 'managed'; any failure ⇒ 'failed' + reason. This is
    the only code path that may set an ssh node to 'managed'."""
    node_id = spec.get('node_id')
    node = db.query('SELECT * FROM nodes WHERE id=?', (node_id,), one=True)
    if not node:
        log.write(f'[ccp] node id={node_id} no longer exists\n')
        return 2
    addr, user, port = node['address'], node['ssh_user'], node['ssh_port']
    mode = spec.get('mode', 'onboard')

    def fail(reason):
        log.write(f'FAILED: {reason}\n')
        _set_node_state(node_id, 'failed', reason)
        return 1

    try:
        if mode == 'onboard':
            if not secret:
                return fail('no password supplied to the onboarding job')

            log.write(f'[1/3] validating credentials for {user}@{addr}:{port} …\n')
            rc, out = _password_ssh(addr, user, port, 'true', secret)
            if rc != 0:
                return fail(_auth_failure_reason(rc, out))
            log.write('      credentials OK\n')

            pub = public_key()
            if not pub:
                return fail(f'CCP has no SSH public key at {SSH_KEY}.pub '
                            '(key generation failed at startup?)')
            log.write('[2/3] installing the CCP public key …\n')
            qpub = shlex.quote(pub)
            install = ('mkdir -p ~/.ssh && chmod 700 ~/.ssh && '
                       'touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && '
                       f'(grep -qxF {qpub} ~/.ssh/authorized_keys || '
                       f'printf %s\\\\n {qpub} >> ~/.ssh/authorized_keys)')
            rc, out = _password_ssh(addr, user, port, install, secret)
            if rc != 0:
                return fail(f'key bootstrap failed (exit {rc}): {out.strip()[:200]}')
            log.write('      key installed (idempotent)\n')
        else:
            log.write(f'[verify] checking key access for {user}@{addr}:{port} …\n')

        step = '[3/3]' if mode == 'onboard' else '[verify]'
        log.write(f'{step} confirming command execution over key auth …\n')
        rc, out = _key_ssh(addr, user, port, 'echo CCP_OK && hostname')
        lines = [l.strip() for l in out.splitlines() if l.strip()]
        if rc != 0 or 'CCP_OK' not in lines:
            return fail(f'key-auth execution check failed (exit {rc}): '
                        f'{(lines[-1] if lines else "no output")[:200]}')
        remote_name = lines[-1] if lines[-1] != 'CCP_OK' else ''
        log.write(f'      command execution OK (remote hostname: {remote_name or "?"})\n')

        # Apply the operator's chosen hostname to the node itself, so the
        # machine, the inventory and the Slurm NodeName all agree from the
        # start. Without this a node keeps a default name like 'ubuntu' and
        # the Slurm config generated later never matches it.
        want = db.query('SELECT name FROM nodes WHERE id=?', (node_id,), one=True)['name']
        if (spec.get('set_hostname') and not spec.get('auto_name')
                and remote_name and want != remote_name):
            log.write(f'[ccp] setting the node hostname to {want} '
                      f'(currently {remote_name}) …\n')
            hrc, hout = _key_ssh(addr, user, port, _hostname_script(want))
            log.write(hout.rstrip() + '\n')
            got = ''
            for line in hout.splitlines():
                if line.startswith('CCP_HOSTNAME_ACTUAL '):
                    got = line.split(' ', 1)[1].strip()
            if hrc == 0 and got == want:
                log.write(f'      hostname set to {want}\n')
                remote_name = want
            else:
                # never let CCP hold a name the box does not answer to: adopt
                # the machine's real hostname instead and say why
                log.write(f'[ccp] could not set the hostname (node still reports '
                          f'{got or remote_name}).\n')
                if '_' in want:
                    log.write('[ccp] note: systemd rejects underscores in '
                              f'hostnames on many releases — try '
                              f'{want.replace("_", "-")}\n')
                if (_SAFE_NAME.fullmatch(remote_name) and not db.query(
                        'SELECT 1 FROM nodes WHERE name=? AND id<>?',
                        (remote_name, node_id), one=True)):
                    db.execute('UPDATE nodes SET name=? WHERE id=?',
                               (remote_name, node_id))
                    topology.apply(node_id, remote_name)
                    log.write(f'[ccp] inventory now uses the node\'s real name '
                              f'{remote_name}; rename it from the Nodes page once '
                              'the hostname can be set\n')

        # adopt the node's real hostname when the row was created nameless
        if spec.get('auto_name') and remote_name and _SAFE_NAME.fullmatch(remote_name):
            clash = db.query('SELECT id FROM nodes WHERE name=? AND id<>?',
                             (remote_name, node_id), one=True)
            if clash:
                log.write(f'      keeping placeholder name (another node is already '
                          f'called {remote_name})\n')
            else:
                db.execute('UPDATE nodes SET name=? WHERE id=?', (remote_name, node_id))
                topology.apply(node_id, remote_name)
                log.write(f'      node renamed to {remote_name}\n')

        db.execute("UPDATE nodes SET state='managed', state_detail='', "
                   'onboarded_at=? WHERE id=?', (int(time.time()), node_id))
        log.write('MANAGED: node passed credential validation, key bootstrap '
                  'and execution check\n')

        # chain hardware discovery so facts are fresh the moment a node lands
        final_name = db.query('SELECT name FROM nodes WHERE id=?',
                              (node_id,), one=True)['name']
        creator = db.query('SELECT created_by FROM jobs WHERE id=?',
                           (job_id,), one=True)['created_by']
        hw_job = start_job('hwscan', final_name, {'node_id': node_id}, creator)
        log.write(f'[ccp] queued hardware discovery (job {hw_job})\n')
        return 0
    except Exception as exc:
        # never leave the row stuck in 'onboarding'
        return fail(f'onboarding crashed: {exc}')


# ── hostname management ──────────────────────────────────────────────────────

def _hostname_script(new_name):
    """Remote script: set the hostname and prove it took effect.

    hostnamectl is tried first but is NOT trusted — systemd refuses names it
    considers invalid (an underscore, for one), hostnamed may be unavailable,
    and a static-only change can leave the running name untouched. So on any
    hostnamectl failure we fall back to /etc/hostname + sethostname(2), keep
    /etc/hosts consistent, stop cloud-init from reverting the name on the next
    boot, and finally re-read the live hostname and exit non-zero unless it
    equals what was asked for. The caller only updates the inventory when this
    script proves the change — CCP's name must never disagree with the box, or
    Slurm's identity checks break."""
    q = shlex.quote(new_name)
    return f'''
new={q}
old="$(hostname)"
if [ "$(id -u)" != 0 ]; then
  if command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then SUDO="sudo -n";
  else echo "CCP_ERR: not root and passwordless sudo unavailable"; exit 40; fi
else SUDO=""; fi
set_ok=0
if command -v hostnamectl >/dev/null 2>&1; then
  if $SUDO hostnamectl set-hostname "$new" 2>&1; then set_ok=1
  else echo "CCP_NOTE: hostnamectl refused the name — falling back to /etc/hostname"; fi
fi
if [ "$set_ok" != 1 ]; then
  printf '%s\\n' "$new" | $SUDO tee /etc/hostname >/dev/null || exit 41
  $SUDO hostname "$new" 2>&1 || exit 41
fi
# Rewrite the canonical 127.0.1.1 entry in place and collapse any duplicates
# (an earlier CCP version appended a new line per rename, leaving the old name
# and several 127.0.1.1 rows behind — this cleans that up). Everything else in
# the file, including peer and IPv6 entries, is preserved byte for byte.
tmp_hosts=$(mktemp) || exit 42
awk -v new="$new" '
  $1 == "127.0.1.1" {{ if (!done) {{ print "127.0.1.1\\t" new; done = 1 }} next }}
  {{ print }}
  END {{ if (!done) print "127.0.1.1\\t" new }}
' /etc/hosts > "$tmp_hosts" || {{ rm -f "$tmp_hosts"; exit 42; }}
if ! grep -q "[[:space:]]$new\\$" "$tmp_hosts"; then rm -f "$tmp_hosts"; exit 42; fi
$SUDO cp "$tmp_hosts" /etc/hosts || {{ rm -f "$tmp_hosts"; exit 42; }}
rm -f "$tmp_hosts"
# cloud images rewrite the hostname on every boot unless told not to
if [ -f /etc/cloud/cloud.cfg ]; then
  if grep -q '^preserve_hostname:' /etc/cloud/cloud.cfg 2>/dev/null; then
    $SUDO sed -i 's/^preserve_hostname:.*/preserve_hostname: true/' /etc/cloud/cloud.cfg
  else
    printf 'preserve_hostname: true\\n' | $SUDO tee -a /etc/cloud/cloud.cfg >/dev/null
  fi
fi
actual="$(hostname)"
echo "CCP_HOSTNAME_ACTUAL $actual"
if [ "$actual" != "$new" ]; then
  echo "CCP_ERR: hostname is still '$actual' after the change"
  exit 43
fi
echo "CCP_HOSTNAME_OK $actual"
'''


def _run_hostname(job_id, spec, log):
    """One-click hostname change: apply on the node, verify, then refresh the
    inventory (name + derived rack/sled/role) immediately."""
    node = db.query('SELECT * FROM nodes WHERE id=?', (spec.get('node_id'),), one=True)
    new_name = spec.get('new_name') or ''
    if not node:
        log.write('[ccp] node no longer exists\n')
        return 2
    if not _SAFE_NAME.fullmatch(new_name):
        log.write(f'[ccp] invalid hostname {new_name!r}\n')
        return 2
    log.write(f'[ccp] renaming {node["name"]} → {new_name} on {node["address"]} …\n')
    if node['conn'] == 'local':
        log.write('[ccp] refusing to rename the CCP host itself from the panel\n')
        return 2
    rc, out = _key_ssh(node['address'], node['ssh_user'], node['ssh_port'],
                       _hostname_script(new_name))
    log.write(out + '\n')

    # What the box reports it is actually called now — the only thing we trust.
    actual = ''
    for line in out.splitlines():
        if line.startswith('CCP_HOSTNAME_ACTUAL '):
            actual = line.split(' ', 1)[1].strip()

    if rc != 0 or f'CCP_HOSTNAME_OK {new_name}' not in out or actual != new_name:
        hints = {40: 'the SSH user needs root or passwordless sudo',
                 41: 'setting the hostname failed',
                 42: 'updating /etc/hosts failed',
                 43: f'the node still reports {actual or "its old name"}'}
        log.write(f'[ccp] hostname change failed (exit {rc})'
                  f'{": " + hints[rc] if rc in hints else ""}\n')
        if '_' in new_name:
            log.write('[ccp] note: systemd rejects underscores in hostnames on '
                      'many releases — try the hyphen form '
                      f'({new_name.replace("_", "-")}), which CCP parses into '
                      'the same rack/sled/role topology\n')
        log.write(f'[ccp] inventory left unchanged: {node["name"]} — CCP never '
                  'records a name the node does not actually have (Slurm '
                  'identity checks depend on the two matching)\n')
        return 1
    db.execute('UPDATE nodes SET name=? WHERE id=?', (new_name, node['id']))
    t = topology.apply(node['id'], new_name)
    if t['rack'] is not None:
        placed = f"rack={t['rack']} sled={t['sled']} role={t['role'] or '-'}"
    else:
        placed = 'no rack topology in name'
    log.write(f'[ccp] inventory updated: name={new_name} ({placed})\n')
    return 0


# ── hardware discovery ───────────────────────────────────────────────────────

# POSIX sh, no dependencies beyond coreutils/iproute2; every section degrades
# to nothing rather than failing. Output is key=value lines between markers so
# parse_facts() never has to guess at free-form text.
FACT_SCRIPT = r'''
export LC_ALL=C
echo CCP_FACTS_BEGIN
echo "hostname=$(hostname)"
if [ -r /etc/os-release ]; then . /etc/os-release; echo "os_name=${PRETTY_NAME:-unknown}"; fi
echo "kernel=$(uname -r 2>/dev/null)"
echo "arch=$(uname -m 2>/dev/null)"
if command -v lscpu >/dev/null 2>&1; then
  lscpu 2>/dev/null | sed -n \
    -e 's/^Model name:[[:space:]]*/cpu_model=/p' \
    -e 's/^Socket(s):[[:space:]]*/cpu_sockets=/p' \
    -e 's/^Thread(s) per core:[[:space:]]*/threads_per_core=/p' \
    -e 's/^CPU(s):[[:space:]]*/cpu_cores=/p'
else
  echo "cpu_model=$(sed -n 's/^model name[[:space:]]*: //p' /proc/cpuinfo 2>/dev/null | head -1)"
  echo "cpu_cores=$(nproc 2>/dev/null || grep -c ^processor /proc/cpuinfo 2>/dev/null)"
fi
echo "mem_kb=$(sed -n 's/^MemTotal:[[:space:]]*\([0-9]*\).*/\1/p' /proc/meminfo 2>/dev/null)"
lsblk -dnb -o NAME,SIZE,TYPE 2>/dev/null | awk '$3=="disk"{print "disk="$1"|"$2}'
ip -o -4 addr show scope global 2>/dev/null | awk '{split($4,a,"/"); print "nic="$2"|"a[1]}'
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | sed 's/^/gpu=/'
else
  lspci 2>/dev/null | grep -iE 'vga|3d controller|display' | sed 's/^/pci_gpu=/'
fi
if command -v ibstat >/dev/null 2>&1; then ibstat -l 2>/dev/null | sed 's/^/ib=/'; fi
echo CCP_FACTS_END
'''

_GPU_PCI_RE = re.compile(r'\b(nvidia|amd|ati|instinct|habana|gaudi)\b', re.I)


def _fmt_bytes(n):
    for unit in ('B', 'KB', 'MB', 'GB', 'TB', 'PB'):
        if n < 1000 or unit == 'PB':
            return (f'{n:.1f}'.rstrip('0').rstrip('.') if unit != 'B'
                    else str(int(n))) + unit
        n /= 1000
    return str(n)


def parse_facts(text):
    """Parse FACT_SCRIPT output into a hardware-row dict (summary columns +
    raw dict). Tolerates missing sections and junk around the markers."""
    lines = []
    inside = False
    for line in text.splitlines():
        line = line.strip()
        if line == 'CCP_FACTS_BEGIN':
            inside, lines = True, []
        elif line == 'CCP_FACTS_END':
            inside = False
        elif inside and '=' in line:
            lines.append(line)
    raw = {}
    for line in lines:
        k, _, v = line.partition('=')
        raw.setdefault(k, []).append(v.strip())

    def first(key, default=''):
        return raw.get(key, [default])[0]

    def intval(key):
        try:
            return int(first(key))
        except ValueError:
            return None

    disks = []
    for d in raw.get('disk', []):
        name, _, size = d.partition('|')
        try:
            disks.append(f'{name} {_fmt_bytes(int(size))}')
        except ValueError:
            disks.append(name)
    nics = [f'{n.split("|")[0]} {n.split("|")[1]}' if '|' in n else n
            for n in raw.get('nic', [])]

    gpus = raw.get('gpu', [])
    if not gpus:   # no nvidia-smi: fall back to GPU-looking PCI devices
        gpus = [p.split(': ', 1)[-1] for p in raw.get('pci_gpu', [])
                if _GPU_PCI_RE.search(p)]
    mem_kb = intval('mem_kb')
    return {
        'os_hostname': first('hostname'),
        'os_name': first('os_name'), 'kernel': first('kernel'),
        'cpu_model': first('cpu_model'), 'cpu_sockets': intval('cpu_sockets'),
        'cpu_cores': intval('cpu_cores'),
        'threads_per_core': intval('threads_per_core'),
        'mem_mb': mem_kb // 1024 if mem_kb else None,
        'disks': ', '.join(disks), 'nics': ', '.join(nics),
        'gpu_count': len(gpus), 'gpu_model': gpus[0] if gpus else '',
        'infiniband': ', '.join(raw.get('ib', [])),
        'raw': raw,
    }


def save_hardware(node_id, facts):
    db.execute(
        'INSERT INTO hardware (node_id, cpu_model, cpu_sockets, cpu_cores, '
        'threads_per_core, mem_mb, disks, nics, gpu_count, gpu_model, os_name, '
        'kernel, infiniband, raw_json, updated_at) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) '
        'ON CONFLICT(node_id) DO UPDATE SET cpu_model=excluded.cpu_model, '
        'cpu_sockets=excluded.cpu_sockets, cpu_cores=excluded.cpu_cores, '
        'threads_per_core=excluded.threads_per_core, mem_mb=excluded.mem_mb, '
        'disks=excluded.disks, nics=excluded.nics, gpu_count=excluded.gpu_count, '
        'gpu_model=excluded.gpu_model, os_name=excluded.os_name, '
        'kernel=excluded.kernel, infiniband=excluded.infiniband, '
        'raw_json=excluded.raw_json, updated_at=excluded.updated_at',
        (node_id, facts['cpu_model'], facts['cpu_sockets'], facts['cpu_cores'],
         facts['threads_per_core'], facts['mem_mb'], facts['disks'],
         facts['nics'], facts['gpu_count'], facts['gpu_model'],
         facts['os_name'], facts['kernel'], facts['infiniband'],
         json.dumps(facts['raw']), int(time.time())))


def _run_hwscan(job_id, spec, log):
    """Collect hardware facts from one node and persist them."""
    node = db.query('SELECT * FROM nodes WHERE id=?', (spec.get('node_id'),), one=True)
    if not node:
        log.write('[ccp] node no longer exists\n')
        return 2
    if not node_eligible(node):
        log.write(f'[ccp] node {node["name"]} is {node["state"]} — '
                  'only managed nodes can be scanned\n')
        return 2
    log.write(f'[ccp] collecting hardware facts from {node["name"]} …\n')
    if node['conn'] == 'local':
        try:
            p = subprocess.run(['/bin/sh', '-c', FACT_SCRIPT], capture_output=True,
                               text=True, timeout=ONBOARD_STEP_TIMEOUT)
            rc, out = p.returncode, (p.stdout or '') + (p.stderr or '')
        except subprocess.TimeoutExpired:
            rc, out = -1, 'timed out'
    else:
        rc, out = _key_ssh(node['address'], node['ssh_user'], node['ssh_port'],
                           FACT_SCRIPT)
    log.write(out + '\n')
    if 'CCP_FACTS_BEGIN' not in out:
        log.write(f'[ccp] fact collection failed (exit {rc})\n')
        return 1
    facts = parse_facts(out)
    save_hardware(node['id'], facts)
    # Inventory name vs the box's real hostname. Slurm identity no longer
    # depends on them matching (slurmd is pinned with -N and SlurmctldHost is
    # resolved from live facts), but drift means the rack view and NodeNames
    # describe a name the machine doesn't answer to — say so plainly.
    real = facts.get('os_hostname') or ''
    if real and node['conn'] != 'local' and real != node['name']:
        log.write(f'[ccp] NOTE: inventory calls this node {node["name"]} but the '
                  f'machine reports its hostname as {real}. Use Rename on the '
                  'Nodes page to make them agree (a failed earlier rename is '
                  'the usual cause).\n')
    log.write(f'[ccp] saved: {facts["cpu_cores"] or "?"} CPUs, '
              f'{facts["mem_mb"] or "?"} MB RAM, {facts["gpu_count"]} GPU(s)'
              f'{" (" + facts["gpu_model"] + ")" if facts["gpu_model"] else ""}, '
              f'{facts["os_name"] or "unknown OS"}\n')
    return 0


# ── file deployment (staging → nodes) ────────────────────────────────────────
# Two strategies over the same target selection: clush --copy for raw parallel
# delivery, or the Ansible copy module (idempotent, reports changed vs ok).
# Output is emitted with '##GROUP##' / '===== host (addr) | STATUS: X ====='
# markers so the UI renders it as nested group → host collapsibles.

def _group_of(node):
    """First infrastructure group of a node, for output grouping."""
    for g in (node['groups'] or '').split(','):
        if g.strip():
            return g.strip()
    return 'ungrouped'


def _run_filedeploy(job_id, spec, log):
    """Copy staged files to selected nodes. spec: {node_ids, srcs (absolute,
    validated by the API), dest, method}."""
    nodes = _resolve_nodes(spec.get('node_ids', []), log)
    srcs = [s for s in spec.get('srcs', []) if os.path.exists(s)]
    dest = spec.get('dest') or ''
    method = 'ansible' if spec.get('method') == 'ansible' else 'clush'
    if not nodes:
        log.write('[ccp] no managed target nodes resolved\n')
        return 2
    if not srcs:
        log.write('[ccp] none of the selected files exist any more\n')
        return 2
    log.write(f'[ccp] deploying {len(srcs)} file(s) to {len(nodes)} node(s) '
              f'at {dest} via {method}\n')
    for s in srcs:
        try:
            log.write(f'[ccp]   {os.path.basename(s)} '
                      f'({_fmt_bytes(os.path.getsize(s))})\n')
        except OSError:
            log.write(f'[ccp]   {os.path.basename(s)}\n')
    log.write('\n')
    if method == 'ansible':
        return _filedeploy_ansible(nodes, srcs, dest, log)
    return _filedeploy_clush(nodes, srcs, dest, log)


def _filedeploy_clush(nodes, srcs, dest, log):
    """clush -w <nodelist> --copy <src...> --dest <dest>, one run per
    (user, port) bucket, plus direct cp for local nodes."""
    worst = 0
    by_group = {}
    for n in nodes:
        by_group.setdefault(_group_of(n), []).append(n)

    for group, members in sorted(by_group.items()):
        log.write(f'##GROUP## {group}\n')
        local = [n for n in members if n['conn'] == 'local']
        remote = [n for n in members if n['conn'] != 'local']

        for n in local:
            rc, out = 0, ''
            try:
                os.makedirs(dest, exist_ok=True)
                p = subprocess.run(['cp', '-v', *srcs, dest],
                                   capture_output=True, text=True,
                                   timeout=JOB_TIMEOUT)
                rc, out = p.returncode, (p.stdout or '') + (p.stderr or '')
            except Exception as exc:
                rc, out = 1, str(exc)
            status = 'CHANGED' if rc == 0 else 'FAILED'
            log.write(f'===== {n["name"]} ({n["address"]}, local) | '
                      f'STATUS: {status} =====\n{out.rstrip()}\n'
                      f'[{n["name"]} exit {rc}]\n\n')
            worst = max(worst, rc)

        if not remote:
            continue
        buckets = {}
        for n in remote:
            buckets.setdefault((n['ssh_user'], n['ssh_port']), []).append(n)
        for (user, port), bnodes in buckets.items():
            nodelist = ','.join(n['address'] for n in bnodes)
            cmd = ['clush', '-w', nodelist, '-u', str(JOB_TIMEOUT),
                   '--user', user]
            opts = list(SSH_COMMON) + ['-p', str(port)]
            if os.path.exists(SSH_KEY):
                opts += ['-i', SSH_KEY]
            cmd += ['-o', ' '.join(shlex.quote(o) for o in opts)]
            cmd += ['--copy', *srcs, '--dest', dest]
            log.write(f'[ccp] {" ".join(shlex.quote(c) for c in cmd)}\n')
            try:
                p = subprocess.run(cmd, capture_output=True, text=True,
                                   timeout=JOB_TIMEOUT + 30)
                rc, out = p.returncode, (p.stdout or '') + (p.stderr or '')
            except FileNotFoundError:
                rc, out = 1, 'clush (ClusterShell) not installed in the CCP container'
            except subprocess.TimeoutExpired:
                rc, out = 124, f'clush timed out after {JOB_TIMEOUT}s'
            # clush --copy reports failures per node; attribute them where we can
            for n in bnodes:
                mine = '\n'.join(l for l in out.splitlines()
                                 if n['address'] in l or n['name'] in l)
                nrc = 0 if rc == 0 else (1 if mine or rc != 0 else 0)
                status = 'CHANGED' if nrc == 0 else 'FAILED'
                body = mine or (f'copied {len(srcs)} file(s) to {dest}'
                                if nrc == 0 else out.strip())
                log.write(f'===== {n["name"]} ({n["address"]}, ssh {user}@:{port}) | '
                          f'STATUS: {status} =====\n{body.rstrip()}\n'
                          f'[{n["name"]} exit {nrc}]\n\n')
            worst = max(worst, rc)
    return worst


def _filedeploy_ansible(nodes, srcs, dest, log):
    """Ansible copy module via a generated inventory — idempotent, and it
    distinguishes changed from ok per host."""
    with tempfile.TemporaryDirectory() as tmp:
        inv_path = os.path.join(tmp, 'inventory.ini')
        with open(inv_path, 'w') as inv:
            groups = {}
            inv.write('[all]\n')
            for n in nodes:
                if n['conn'] == 'local':
                    inv.write(f'{n["name"]} ansible_connection=local\n')
                else:
                    line = (f'{n["name"]} ansible_host={n["address"]} '
                            f'ansible_user={n["ssh_user"]} ansible_port={n["ssh_port"]}')
                    if os.path.exists(SSH_KEY):
                        line += f' ansible_ssh_private_key_file={SSH_KEY}'
                    inv.write(line + '\n')
                groups.setdefault(_group_of(n), []).append(n['name'])
            for g, names in groups.items():
                safe = re.sub(r'[^A-Za-z0-9_]', '_', g)
                inv.write(f'\n[{safe}]\n' + '\n'.join(names) + '\n')

        pb_path = os.path.join(tmp, 'deploy.yml')
        files_yaml = '\n'.join(f'    - {shlex.quote(s)}' for s in srcs)
        with open(pb_path, 'w') as pb:
            pb.write(f'''---
- name: Deploy staged files (CCP)
  hosts: all
  become: true
  gather_facts: false
  vars:
    ccp_dest: {shlex.quote(dest)}
    ccp_files:
{files_yaml}
  tasks:
    - name: Ensure the destination directory exists
      ansible.builtin.file:
        path: "{{{{ ccp_dest }}}}"
        state: directory
      when: ccp_dest is search('/$') or true

    - name: Copy the staged files
      ansible.builtin.copy:
        src: "{{{{ item }}}}"
        dest: "{{{{ ccp_dest }}}}"
        mode: preserve
      loop: "{{{{ ccp_files }}}}"
''')
        cmd = ['ansible-playbook', '-i', inv_path, pb_path]
        env = dict(os.environ, ANSIBLE_HOST_KEY_CHECKING='False',
                   ANSIBLE_FORCE_COLOR='0', ANSIBLE_RETRY_FILES_ENABLED='False',
                   ANSIBLE_LOCAL_TEMP='/tmp/.ansible-ccp',
                   ANSIBLE_STDOUT_CALLBACK='default')
        log.write(f'[ccp] {" ".join(shlex.quote(c) for c in cmd)}\n\n')
        try:
            p = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=JOB_TIMEOUT, env=env)
            rc, out = p.returncode, (p.stdout or '') + (p.stderr or '')
        except FileNotFoundError:
            log.write('[ccp] ansible-playbook not installed in the CCP container\n')
            return 1
        except subprocess.TimeoutExpired:
            log.write(f'[ccp] ansible timed out after {JOB_TIMEOUT}s\n')
            return 124

        # per-host status from the recap, output grouped by infra group
        recap = {}
        for m in re.finditer(r'^(\S+)\s*:\s*ok=(\d+)\s+changed=(\d+).*?'
                             r'failed=(\d+)', out, re.MULTILINE):
            recap[m.group(1)] = {'changed': int(m.group(3)),
                                 'failed': int(m.group(4))}
        by_group = {}
        for n in nodes:
            by_group.setdefault(_group_of(n), []).append(n)
        for group, members in sorted(by_group.items()):
            log.write(f'##GROUP## {group}\n')
            for n in members:
                r = recap.get(n['name'], {})
                if not r:
                    status, nrc = 'FAILED', 1
                elif r['failed']:
                    status, nrc = 'FAILED', 1
                elif r['changed']:
                    status, nrc = 'CHANGED', 0
                else:
                    status, nrc = 'SUCCESS', 0
                mine = '\n'.join(l for l in out.splitlines()
                                 if n['name'] in l)
                log.write(f'===== {n["name"]} ({n["address"]}) | STATUS: {status} '
                          f'=====\n{mine.rstrip() or "(no per-host output)"}\n'
                          f'[{n["name"]} exit {nrc}]\n\n')
        log.write('##GROUP## ansible run log\n')
        log.write(f'===== ansible-playbook (recap) | STATUS: '
                  f'{"SUCCESS" if rc == 0 else "FAILED"} =====\n{out.rstrip()}\n'
                  f'[ansible-playbook exit {rc}]\n')
        return rc


# ── slurm lifecycle stages ───────────────────────────────────────────────────
# validate / benchmark / monitor run real commands on the controller over key
# auth; report aggregates locally. Deployment and cleanup are Ansible jobs
# (kind slurm_deploy) — see slurm.py. Each stage advances clusters.slurm_state
# via the generic advance_to hook in _run() only on success.

def _slurm_ssh(node, command, log, label=None, timeout=None):
    """Run one command on a slurm node over key auth, framing its output with
    a per-host header (same '=====' style as ClusterShell) and an exit footer,
    so the job log always says which node — and which check — produced each
    block."""
    log.write(f'===== {node["name"]} ({node["address"]}) : {label or command} =====\n')
    kw = {'timeout': timeout} if timeout else {}
    rc, out = _key_ssh(node['address'], node['ssh_user'], node['ssh_port'], command, **kw)
    log.write(out.rstrip() + '\n')
    log.write(f'[{node["name"]} exit {rc}]\n\n')
    return rc


# Everything a person debugging "slurmd would not start" asks for, gathered
# from one node in one SSH round trip: identity, binaries, service states,
# journals, the effective unit with drop-ins, every config file, the hardware
# as slurmd sees it, ports, hosts, GPUs — and, when slurmd is down, a short
# foreground run that prints the daemon's own fatal. __NAME__ is the CCP
# inventory name (= NodeName), __CTLN__/__CTL__ the controller-only parts.
DIAG_SCRIPT = r'''SUDO=""; [ "$(id -u)" = 0 ] || SUDO="sudo -n"
sec(){ printf '\n### %s\n' "$*"; }
sec "host"
echo "hostname: $(hostname)   inventory name: __NAME__"
grep -E '^PRETTY_NAME=' /etc/os-release 2>/dev/null
echo "cpus: $(nproc)   mem: $(awk '/^MemTotal/{printf "%d", $2/1024}' /proc/meminfo) MB   cgroup: $(stat -fc %T /sys/fs/cgroup 2>&1)"
sec "slurm binaries"
for b in slurmd slurmctld slurmstepd sbatch srun sinfo; do printf '%-11s %s\n' "$b" "$(command -v $b 2>/dev/null || echo MISSING)"; done
slurmd -V 2>&1
sec "services"
for s in munge slurmd slurmctld; do printf '%-9s active=%-8s enabled=%s\n' "$s" "$($SUDO systemctl is-active $s 2>&1)" "$($SUDO systemctl is-enabled $s 2>&1)"; done
sec "systemctl status slurmd"; $SUDO systemctl status slurmd --no-pager -l 2>&1 | head -25
sec "journalctl -u slurmd -n 60"; $SUDO journalctl -u slurmd -n 60 --no-pager 2>&1
sec "journalctl -u slurmctld -n __CTLN__"; $SUDO journalctl -u slurmctld -n __CTLN__ --no-pager 2>&1
sec "journalctl -u munge -n 10"; $SUDO journalctl -u munge -n 10 --no-pager 2>&1
sec "munge round trip"; munge -n 2>&1 | unmunge 2>&1 | head -3
sec "effective slurmd unit + drop-ins (systemctl cat slurmd)"; $SUDO systemctl cat slurmd 2>&1
sec "/etc/default/slurmd"; cat /etc/default/slurmd 2>&1
sec "/etc/slurm"; ls -la /etc/slurm 2>&1
sec "slurm.conf"; $SUDO cat /etc/slurm/slurm.conf 2>&1
sec "gres.conf"; $SUDO cat /etc/slurm/gres.conf 2>&1
sec "cgroup.conf"; $SUDO cat /etc/slurm/cgroup.conf 2>&1
sec "slurmd -C (hardware as slurmd sees it)"; $SUDO slurmd -C 2>&1 | head -4
sec "spool and log dirs"; ls -ld /var/spool/slurmd /var/spool/slurmctld /var/log/slurm 2>&1
sec "slurmd.log tail"; $SUDO tail -n 25 /var/log/slurm/slurmd.log 2>&1
sec "listening on 6817/6818"; (ss -ltnp 2>/dev/null || netstat -ltnp 2>/dev/null) | grep -E ':(6817|6818)\b' || echo "(nothing listening on 6817/6818)"
sec "/etc/hosts"; cat /etc/hosts 2>&1
sec "GPU"; nvidia-smi -L 2>&1 | head -8; ls -l /dev/nvidia* 2>&1 | head -4
if ! $SUDO systemctl is-active -q slurmd 2>/dev/null; then
  sec "foreground probe (8 s): slurmd -D -vv -N __NAME__ -- the daemon's own reason"
  $SUDO timeout 8 slurmd -D -vv -N __NAME__ 2>&1 | tail -30
fi
__CTL__
exit 0
'''

DIAG_CONTROLLER = r'''sec "systemctl status slurmctld"; $SUDO systemctl status slurmctld --no-pager -l 2>&1 | head -25
sec "slurmctld.log tail"; $SUDO tail -n 25 /var/log/slurm/slurmctld.log 2>&1
sec "scontrol ping"; scontrol ping 2>&1
sec "sinfo -N -l"; sinfo -N -l 2>&1
sec "squeue"; squeue 2>&1
if ! $SUDO systemctl is-active -q slurmctld 2>/dev/null; then
  sec "foreground probe (8 s): slurmctld -D -vv as user slurm"
  $SUDO runuser -u slurm -- timeout 8 slurmctld -D -vv 2>&1 | tail -30
fi'''


def _slurm_diagnose(controller, members, log):
    """Collect the diagnostics bundle from every member (controller extras on
    the controller), one '=====' frame per node, so the whole job log can be
    handed to whoever is debugging. Works in any lifecycle state and changes
    nothing on the nodes (the foreground probes run only while the daemon is
    down and are killed after 8 s)."""
    if not members:
        log.write('[ccp] no managed members\n')
        return 2
    cid = controller['id'] if controller else None
    log.write(f'[ccp] collecting logs from {len(members)} node(s)'
              f'{" — controller " + controller["name"] if controller else " — no controller set yet"}\n\n')
    worst = 0
    for m in members:
        is_ctl = m['id'] == cid
        script = (DIAG_SCRIPT.replace('__NAME__', shlex.quote(m['name']))
                  .replace('__CTLN__', '40' if is_ctl else '10')
                  .replace('__CTL__', DIAG_CONTROLLER if is_ctl else ''))
        worst = max(worst, _slurm_ssh(
            m, f"bash -s <<'CCPDIAG'\n{script}CCPDIAG\n", log,
            'diagnostics bundle' + (' (controller)' if is_ctl else ''),
            timeout=150))
    log.write(f'DIAGNOSTICS COLLECTED from {len(members)} node(s) — use "Copy log" '
              'or "Download log" on this page and paste it when asking for help.\n')
    return worst


def _run_slurm_action(job_id, spec, log):
    stage = spec.get('stage')
    members = _resolve_nodes(spec.get('node_ids', []), log)
    if stage == 'report':
        return _slurm_report(spec, members, log)
    controller = db.query('SELECT * FROM nodes WHERE id=?',
                          (spec.get('controller_id'),), one=True)
    if stage == 'diagnose':
        return _slurm_diagnose(
            controller if controller and node_eligible(controller) else None,
            members, log)
    if not controller or not node_eligible(controller):
        log.write('[ccp] controller node is missing or not managed\n')
        return 2
    if not members:
        log.write('[ccp] no managed members\n')
        return 2
    n = len(members)

    log.write(f'[ccp] stage {stage} — controller {controller["name"]} '
              f'({controller["address"]}), {n} member(s)\n\n')

    if stage == 'sbatch':
        return _slurm_sbatch_test(spec, controller, members, n, log)

    if stage == 'validate':
        worst = 0
        worst = max(worst, _slurm_ssh(controller, 'sinfo', log,
                                      'sinfo — partition overview'))
        worst = max(worst, _slurm_ssh(controller, 'sinfo -N -l', log,
                                      'sinfo -N -l — node states'))
        worst = max(worst, _slurm_ssh(
            controller, f'srun -N {n} --ntasks-per-node=1 -t 2 hostname', log,
            f'srun across all {n} node(s) — every hostname must answer'))
        log.write('VALIDATE ' + ('PASSED' if worst == 0 else 'FAILED') + '\n')
        return worst

    if stage == 'monitor':
        worst = 0
        for label, cmd in (('cluster / partition state', 'sinfo'),
                           ('per-node state', 'sinfo -N -l'),
                           ('job queue', 'squeue')):
            worst = max(worst, _slurm_ssh(controller, cmd, log,
                                          f'{label} — {cmd}'))
        return worst

    if stage == 'benchmark':
        worst = 0
        worst = max(worst, _slurm_ssh(
            controller, f'time -p srun -N {n} --ntasks-per-node=1 -t 5 hostname',
            log, f'scheduler dispatch — timed srun across {n} node(s)'))

        others = [m for m in members if m['id'] != controller['id']]
        if others:
            for m in others:
                worst = max(worst, _slurm_ssh(
                    controller, f'ping -c 3 -W 2 {m["address"]}', log,
                    f'latency {controller["name"]} → {m["name"]} ({m["address"]})'))
        else:
            log.write('[ccp] single-node cluster — node-to-node tests need '
                      'at least two members\n\n')

        # bandwidth between two real nodes (never loopback): iperf3 server on
        # one member, client on another, orchestrated over the CCP key
        pair = [m for m in members if m['conn'] == 'ssh'][:2]
        if len(pair) == 2:
            srv, cli = pair
            log.write(f'===== {srv["name"]} ({srv["address"]}) : start iperf3 '
                      f'server =====\n')
            rc, out = _key_ssh(srv['address'], srv['ssh_user'], srv['ssh_port'],
                               'command -v iperf3 >/dev/null 2>&1 && '
                               '(pkill -x iperf3 2>/dev/null; iperf3 -s -1 -D) && '
                               'echo IPERF_SERVER_READY || echo IPERF_MISSING')
            log.write(out.rstrip() + f'\n[{srv["name"]} exit {rc}]\n\n')
            if rc == 0 and 'IPERF_SERVER_READY' in out:
                worst = max(worst, _slurm_ssh(
                    cli, f'iperf3 -c {srv["address"]} -t 5 -f m', log,
                    f'bandwidth {cli["name"]} → {srv["name"]} ({srv["address"]})'))
            else:
                log.write('[ccp] iperf3 not installed on the nodes — bandwidth '
                          'test skipped (apt install iperf3 to enable)\n\n')
        log.write('BENCHMARK ' + ('PASSED' if worst == 0 else 'FAILED') + '\n')
        return worst

    log.write(f'[ccp] unknown slurm stage: {stage}\n')
    return 2


SBATCH_WAIT_SECONDS = int(os.environ.get('CCP_SBATCH_WAIT', '660'))


def _sbatch_script(n, gpu_all):
    """A batch job shaped like an AI training run: one task per node, GPU
    inventory where present, and a timed 'training step' per node (numpy or
    torch when installed, a pure-python loop otherwise), so the same test is
    meaningful on a bare image and on a real GPU node."""
    gres = ('#SBATCH --gres=gpu:1' if gpu_all
            else '# no --gres: not every member declares a GPU')
    return f'''#!/bin/bash
#SBATCH --job-name=ccp-ai-smoke
#SBATCH --nodes={n}
#SBATCH --ntasks-per-node=1
#SBATCH --time=00:10:00
#SBATCH --output=/tmp/ccp-ai-smoke-%j.out
{gres}
echo "== CCP AI-training smoke test: job $SLURM_JOB_ID on $SLURM_JOB_NUM_NODES node(s): $SLURM_JOB_NODELIST =="
srun bash -c 'echo "[$(hostname)] cpus=$(nproc)"; if command -v nvidia-smi >/dev/null 2>&1; then nvidia-smi -L | sed "s/^/[$(hostname)] /"; else echo "[$(hostname)] no NVIDIA GPU visible"; fi'
srun python3 - <<'PY'
import time, socket
h = socket.gethostname()
try:
    import numpy as np
    a = np.random.rand(2048, 2048); t = time.time(); (a @ a).sum(); dt = time.time() - t
    print(f"[{{h}}] training step (numpy matmul 2048x2048): {{dt:.3f}}s")
except ImportError:
    t = time.time(); s = sum(i * i for i in range(3_000_000)); dt = time.time() - t
    print(f"[{{h}}] training step (pure python loop): {{dt:.3f}}s")
try:
    import torch
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    x = torch.randn(1024, 1024, device=dev); t = time.time(); (x @ x).sum().item(); dt = time.time() - t
    print(f"[{{h}}] torch matmul on {{dev}}: {{dt:.3f}}s")
except ImportError:
    print(f"[{{h}}] torch not installed (optional)")
PY
echo "== CCP AI-training smoke test finished =="
'''


def _slurm_sbatch_test(spec, controller, members, n, log):
    """Submit the AI-training-shaped batch job through the real scheduler,
    wait for it, and pull its output back from whichever node ran the batch
    step. Passes only when Slurm reports the job COMPLETED."""
    cluster = db.query('SELECT gres_conf FROM clusters WHERE id=?',
                       (spec.get('cluster_id'),), one=True)
    gres_nodes = set(re.findall(r'NodeName=(\S+)',
                                (cluster['gres_conf'] if cluster else '') or ''))
    gpu_all = bool(members) and all(m['name'] in gres_nodes for m in members)
    script = _sbatch_script(n, gpu_all)
    caddr, cuser, cport = controller['address'], controller['ssh_user'], controller['ssh_port']

    def frame(node, label, rc, out):
        log.write(f'===== {node["name"]} ({node["address"]}) : {label} =====\n'
                  f'{out.rstrip()}\n[{node["name"]} exit {rc}]\n\n')

    log.write(f'[ccp] batch job: {n} node(s), one task each'
              f'{", --gres=gpu:1 on every node" if gpu_all else ", no GPU reservation"}\n\n')
    rc, out = _key_ssh(caddr, cuser, cport,
                       f"cat > /tmp/ccp-ai-smoke.sh <<'CCPEOF'\n{script}CCPEOF\n"
                       'chmod +x /tmp/ccp-ai-smoke.sh && echo "batch script staged"')
    frame(controller, 'stage the batch script', rc, out)
    if rc != 0:
        log.write('SBATCH FAILED: could not stage the script on the controller\n')
        return 1

    rc, out = _key_ssh(caddr, cuser, cport,
                       'sbatch --wait --parsable /tmp/ccp-ai-smoke.sh',
                       timeout=SBATCH_WAIT_SECONDS)
    frame(controller, f'sbatch --wait (up to {SBATCH_WAIT_SECONDS // 60} min)', rc, out)
    jid = ''
    for line in out.splitlines():
        m = re.match(r'^(\d+)', line.strip())
        if m:
            jid = m.group(1)
    if not jid:
        log.write('SBATCH FAILED: sbatch returned no job id — is slurmctld up '
                  'and the partition UP? (run Validate)\n')
        return 1

    rc2, info = _key_ssh(caddr, cuser, cport, f'scontrol show job {jid}')
    frame(controller, f'scontrol show job {jid}', rc2, info)
    m = re.search(r'JobState=(\S+)', info)
    state = m.group(1) if m else 'UNKNOWN'
    m = re.search(r'BatchHost=(\S+)', info)
    batch_host = m.group(1) if m else controller['name']
    target = next((x for x in members if x['name'] == batch_host), controller)

    rc3, out3 = _key_ssh(target['address'], target['ssh_user'], target['ssh_port'],
                         f'cat /tmp/ccp-ai-smoke-{jid}.out')
    frame(target, f'job {jid} output (batch host)', rc3, out3)

    passed = state == 'COMPLETED' and rc == 0
    log.write('SBATCH ' + ('PASSED — the scheduler ran a multi-node batch job '
                           'end to end' if passed
                           else f'FAILED (JobState={state}, sbatch exit {rc})') + '\n')
    return 0 if passed else 1


def _slurm_report(spec, members, log):
    """Aggregate what CCP already knows into a cluster report: membership,
    hardware totals, stored configs, and the latest lifecycle job outcomes."""
    cluster = db.query('SELECT * FROM clusters WHERE id=?',
                       (spec.get('cluster_id'),), one=True)
    if not cluster:
        log.write('[ccp] cluster no longer exists\n')
        return 2
    log.write(f'===== Cluster report: {cluster["name"]} =====\n')
    log.write(f'kind={cluster["kind"]} lifecycle={cluster["slurm_state"]}\n\n')
    total_cpu = total_mem = total_gpu = 0
    gpu_models = {}
    log.write('-- members --\n')
    for m in members:
        hw = db.query('SELECT * FROM hardware WHERE node_id=?', (m['id'],), one=True)
        cpu = (hw['cpu_cores'] if hw else 0) or 0
        mem = (hw['mem_mb'] if hw else 0) or 0
        gpu = (hw['gpu_count'] if hw else 0) or 0
        total_cpu += cpu
        total_mem += mem
        total_gpu += gpu
        if gpu and hw['gpu_model']:
            gpu_models[hw['gpu_model']] = gpu_models.get(hw['gpu_model'], 0) + gpu
        role = ' [controller]' if m['id'] == cluster['controller_node_id'] else ''
        log.write(f'{m["name"]}{role}: {m["address"]} — '
                  f'{cpu or "?"} CPUs, {mem // 1024 if mem else "?"} GB, '
                  f'{gpu} GPU(s), state {m["state"]}\n')
    log.write(f'\n-- capacity --\ntotal: {len(members)} nodes, {total_cpu} CPUs, '
              f'{total_mem // 1024} GB RAM, {total_gpu} GPUs\n')
    for model, count in gpu_models.items():
        log.write(f'  {count}× {model}\n')
    log.write(f'\n-- configuration --\nslurm.conf {"stored" if cluster["slurm_conf"] else "NOT generated"}, '
              f'gres.conf {"stored" if cluster["gres_conf"] else "empty"}\n')
    log.write('\n-- recent lifecycle jobs --\n')
    for j in db.query("SELECT * FROM jobs WHERE kind IN ('slurm_deploy','slurm_action') "
                      'AND target=? ORDER BY id DESC LIMIT 10', (cluster['name'],)):
        spec_j = json.loads(j['spec'] or '{}')
        log.write(f'#{j["id"]} {spec_j.get("stage", j["kind"])}: {j["status"]}'
                  f' ({time.strftime("%Y-%m-%d %H:%M", time.localtime(j["created_at"]))})\n')
    return 0


# ── ansible ──────────────────────────────────────────────────────────────────

def _run_ansible(job_id, spec, log):
    nodes = _resolve_nodes(spec.get('node_ids', []), log)
    playbook = spec.get('playbook', '')
    playbook_path = spec.get('playbook_path', '')   # absolute; API-validated
    extra_vars = spec.get('extra_vars', '')
    if not nodes:
        log.write('[ccp] no target nodes resolved\n')
        return 2
    if not playbook_path and not playbook.strip():
        log.write('[ccp] empty playbook\n')
        return 2

    with tempfile.TemporaryDirectory() as tmp:
        inv_path = os.path.join(tmp, 'inventory.ini')
        with open(inv_path, 'w') as inv:
            inv.write('[all]\n')
            for n in nodes:
                if n['conn'] == 'local':
                    inv.write(f'{n["name"]} ansible_connection=local\n')
                else:
                    line = (f'{n["name"]} ansible_host={n["address"]} '
                            f'ansible_user={n["ssh_user"]} ansible_port={n["ssh_port"]}')
                    if os.path.exists(SSH_KEY):
                        line += f' ansible_ssh_private_key_file={SSH_KEY}'
                    inv.write(line + '\n')
        if playbook_path:
            # filesystem source: run from the playbook's own directory so
            # sibling roles/, group_vars/, ansible.cfg resolve naturally
            pb_path = playbook_path
            workdir = os.path.dirname(playbook_path)
        else:
            pb_path = os.path.join(tmp, 'playbook.yml')
            workdir = tmp
            with open(pb_path, 'w') as pb:
                pb.write(playbook)

        cmd = ['ansible-playbook', '-i', inv_path, pb_path]
        if extra_vars.strip():
            cmd += ['-e', extra_vars]
        env = dict(os.environ,
                   ANSIBLE_HOST_KEY_CHECKING='False',
                   ANSIBLE_FORCE_COLOR='0',
                   ANSIBLE_RETRY_FILES_ENABLED='False',
                   ANSIBLE_LOCAL_TEMP='/tmp/.ansible-ccp',
                   # multi-line results (a failed task's msg, the daemon
                   # diagnostics' stdout_lines) print as readable YAML instead
                   # of one JSON line full of "\n"
                   ANSIBLE_CALLBACK_RESULT_FORMAT='yaml')
        log.write(f'[ccp] {" ".join(shlex.quote(c) for c in cmd)}\n\n')
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, cwd=workdir,
                                stderr=subprocess.STDOUT, text=True, env=env)
        # watchdog: streaming line-by-line blocks until EOF, so a hung playbook
        # is killed out-of-band. Long playbooks (a Slurm source build across
        # the fleet) pass their own budget in spec['timeout'].
        job_timeout = int(spec.get('timeout') or JOB_TIMEOUT)
        timed_out = {'v': False}
        def _kill():
            if proc.poll() is None:
                timed_out['v'] = True
                proc.kill()
        wd = threading.Timer(job_timeout, _kill)
        wd.start()
        try:
            for line in proc.stdout:
                log.write(line)
            proc.wait()
        finally:
            wd.cancel()
        if timed_out['v']:
            log.write(f'\n[ccp] playbook timed out after {job_timeout}s\n')
            return 124
        return proc.returncode


# ── helpers ──────────────────────────────────────────────────────────────────

# Node fields end up in a ClusterShell NodeSet and an Ansible inventory file
# (whitespace, newlines, '=' or NodeSet brackets there would inject arbitrary
# inventory variables/hosts). The API validates them on insert, but the SQLite
# db sits on a host bind mount, so rows can be written outside the API —
# re-check here and refuse to run against anything that doesn't conform.
_SAFE_NAME = re.compile(r'[A-Za-z0-9._-]{1,63}\Z')
_SAFE_ADDR = re.compile(r'[A-Za-z0-9._:-]{1,255}\Z')
_SAFE_USER = re.compile(r'[A-Za-z0-9._-]{1,32}\Z')


def node_eligible(n):
    """Lifecycle gate: only managed ssh nodes (or local nodes) may run jobs."""
    return n['conn'] == 'local' or (n['state'] or '') == 'managed'


def _resolve_nodes(node_ids, log=None):
    if not node_ids:
        return []
    marks = ','.join('?' for _ in node_ids)
    rows = db.query(f'SELECT * FROM nodes WHERE id IN ({marks})', tuple(node_ids))
    nodes = []
    for r in rows:
        n = dict(r)
        try:
            port_ok = 1 <= int(n.get('ssh_port') or 0) <= 65535
        except (TypeError, ValueError):
            port_ok = False
        if not node_eligible(n):
            # the API filters too; re-check here because rows can change (or be
            # written outside the API) between selection and execution
            if log:
                log.write(f'[ccp] skipping node {n.get("name")}: state is '
                          f'{n.get("state")!r} — only managed nodes run jobs\n')
        elif (_SAFE_NAME.fullmatch(str(n.get('name') or ''))
                and _SAFE_ADDR.fullmatch(str(n.get('address') or ''))
                and _SAFE_USER.fullmatch(str(n.get('ssh_user') or ''))
                and port_ok):
            nodes.append(n)
        elif log:
            log.write(f'[ccp] skipping node id={n.get("id")} '
                      f'({n.get("name")!r}): unsafe name/address/user/port\n')
    return nodes
