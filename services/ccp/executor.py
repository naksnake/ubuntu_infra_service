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
              '-o', 'BatchMode=yes']
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
        log.write(f'===== {n["name"]} ({n["address"]}, local) =====\n')
        try:
            p = subprocess.run(['/bin/sh', '-c', command],
                               capture_output=True, text=True, timeout=JOB_TIMEOUT)
            log.write(p.stdout)
            if p.stderr:
                log.write(p.stderr)
            log.write(f'[exit {p.returncode}]\n\n')
            worst = max(worst, p.returncode)
        except subprocess.TimeoutExpired:
            log.write(f'[ccp] command timed out after {JOB_TIMEOUT}s\n[exit 124]\n\n')
            worst = max(worst, 124)

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

        if task.num_timeout():
            worst = max(worst, 124)
            for node in task.iter_keys_timeout():
                log.write(f'[{addr2name.get(str(node), node)} timed out after {JOB_TIMEOUT}s]\n')
        for buf, nodelist in task.iter_buffers():
            for node in nodelist:
                name = addr2name.get(str(node), str(node))
                log.write(f'===== {name} ({node}, ssh {user}@:{port}) =====\n')
                log.write(buf.message().decode('utf-8', 'replace')
                          if hasattr(buf, 'message') else str(buf))
                log.write('\n')
        for rc, nodelist in task.iter_retcodes():
            worst = max(worst, rc)
            for node in nodelist:
                log.write(f'[{addr2name.get(str(node), node)} exit {rc}]\n')
        log.write('\n')
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


def _key_ssh(address, user, port, command):
    """One command over SSH with the CCP key (BatchMode). Returns (rc, output)."""
    cmd = (['ssh'] + list(SSH_COMMON) + ['-i', SSH_KEY, '-p', str(port),
           f'{user}@{address}', command])
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=ONBOARD_STEP_TIMEOUT)
        return p.returncode, (p.stdout or '') + (p.stderr or '')
    except subprocess.TimeoutExpired:
        return -1, f'timed out after {ONBOARD_STEP_TIMEOUT}s'
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
    """Remote script: set the hostname via hostnamectl (fallback to
    /etc/hostname + hostname) and keep /etc/hosts consistent, using sudo -n
    when the SSH user is not root. new_name is validated by the API and
    quoted here."""
    q = shlex.quote(new_name)
    return f'''
new={q}
old="$(hostname)"
if [ "$(id -u)" != 0 ]; then
  if command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then SUDO="sudo -n";
  else echo "CCP_ERR: not root and passwordless sudo unavailable"; exit 40; fi
else SUDO=""; fi
if command -v hostnamectl >/dev/null 2>&1; then
  $SUDO hostnamectl set-hostname "$new" || exit 41
else
  printf '%s\\n' "$new" | $SUDO tee /etc/hostname >/dev/null || exit 41
  $SUDO hostname "$new" || exit 41
fi
esc=$(printf '%s' "$old" | sed 's/[].[^$*\\/]/\\\\&/g')
if [ -n "$old" ] && grep -qw "$old" /etc/hosts 2>/dev/null; then
  $SUDO sed -i "s/\\b$esc\\b/$new/g" /etc/hosts || exit 42
else
  printf '127.0.1.1\\t%s\\n' "$new" | $SUDO tee -a /etc/hosts >/dev/null || exit 42
fi
echo "CCP_HOSTNAME_OK $(hostname)"
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
    if rc != 0 or 'CCP_HOSTNAME_OK' not in out:
        hints = {40: 'the SSH user needs root or passwordless sudo',
                 41: 'setting the hostname failed',
                 42: 'updating /etc/hosts failed'}
        log.write(f'[ccp] hostname change failed (exit {rc})'
                  f'{": " + hints[rc] if rc in hints else ""}\n')
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
    log.write(f'[ccp] saved: {facts["cpu_cores"] or "?"} CPUs, '
              f'{facts["mem_mb"] or "?"} MB RAM, {facts["gpu_count"]} GPU(s)'
              f'{" (" + facts["gpu_model"] + ")" if facts["gpu_model"] else ""}, '
              f'{facts["os_name"] or "unknown OS"}\n')
    return 0


# ── slurm lifecycle stages ───────────────────────────────────────────────────
# validate / benchmark / monitor run real commands on the controller over key
# auth; report aggregates locally. Deployment and cleanup are Ansible jobs
# (kind slurm_deploy) — see slurm.py. Each stage advances clusters.slurm_state
# via the generic advance_to hook in _run() only on success.

def _slurm_ssh(node, command, log, timeout=180):
    rc, out = _key_ssh(node['address'], node['ssh_user'], node['ssh_port'], command)
    log.write(out.rstrip() + '\n')
    if rc != 0:
        log.write(f'[exit {rc}]\n')
    return rc


def _run_slurm_action(job_id, spec, log):
    stage = spec.get('stage')
    members = _resolve_nodes(spec.get('node_ids', []), log)
    if stage == 'report':
        return _slurm_report(spec, members, log)
    controller = db.query('SELECT * FROM nodes WHERE id=?',
                          (spec.get('controller_id'),), one=True)
    if not controller or not node_eligible(controller):
        log.write('[ccp] controller node is missing or not managed\n')
        return 2
    if not members:
        log.write('[ccp] no managed members\n')
        return 2
    n = len(members)

    if stage == 'validate':
        worst = 0
        log.write('== sinfo: partition overview ==\n')
        worst = max(worst, _slurm_ssh(controller, 'sinfo', log))
        log.write('\n== sinfo -N -l: node states ==\n')
        worst = max(worst, _slurm_ssh(controller, 'sinfo -N -l', log))
        log.write(f'\n== srun across all {n} node(s): every hostname must answer ==\n')
        worst = max(worst, _slurm_ssh(
            controller, f'srun -N {n} --ntasks-per-node=1 -t 2 hostname', log))
        log.write('\nVALIDATE ' + ('PASSED' if worst == 0 else 'FAILED') + '\n')
        return worst

    if stage == 'monitor':
        worst = 0
        for title, cmd in (('cluster/partition state', 'sinfo'),
                           ('per-node state', 'sinfo -N -l'),
                           ('queue', 'squeue')):
            log.write(f'== {title}: {cmd} ==\n')
            worst = max(worst, _slurm_ssh(controller, cmd, log))
            log.write('\n')
        return worst

    if stage == 'benchmark':
        worst = 0
        log.write(f'== scheduler dispatch: timed srun across {n} node(s) ==\n')
        worst = max(worst, _slurm_ssh(
            controller,
            f'time -p srun -N {n} --ntasks-per-node=1 -t 5 hostname', log))

        others = [m for m in members if m['id'] != controller['id']]
        if others:
            log.write('\n== node-to-node latency: ping from the controller ==\n')
            for m in others:
                log.write(f'-- {controller["name"]} → {m["name"]} ({m["address"]}) --\n')
                worst = max(worst, _slurm_ssh(
                    controller, f'ping -c 3 -W 2 {m["address"]}', log))
        else:
            log.write('\n[ccp] single-node cluster — node-to-node tests need '
                      'at least two members\n')

        # bandwidth between two real nodes (never loopback): iperf3 server on
        # one member, client on another, orchestrated over the CCP key
        pair = [m for m in members if m['conn'] == 'ssh'][:2]
        if len(pair) == 2:
            srv, cli = pair
            log.write(f'\n== node-to-node bandwidth: iperf3 {cli["name"]} → '
                      f'{srv["name"]} ==\n')
            rc, out = _key_ssh(srv['address'], srv['ssh_user'], srv['ssh_port'],
                               'command -v iperf3 >/dev/null 2>&1 && '
                               '(pkill -x iperf3 2>/dev/null; iperf3 -s -1 -D) && '
                               'echo IPERF_SERVER_READY || echo IPERF_MISSING')
            log.write(out.rstrip() + '\n')
            if rc == 0 and 'IPERF_SERVER_READY' in out:
                worst = max(worst, _slurm_ssh(
                    cli, f'iperf3 -c {srv["address"]} -t 5 -f m', log))
            else:
                log.write('[ccp] iperf3 not installed on the nodes — bandwidth '
                          'test skipped (apt install iperf3 to enable)\n')
        log.write('\nBENCHMARK ' + ('PASSED' if worst == 0 else 'FAILED') + '\n')
        return worst

    log.write(f'[ccp] unknown slurm stage: {stage}\n')
    return 2


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
                   ANSIBLE_LOCAL_TEMP='/tmp/.ansible-ccp')
        log.write(f'[ccp] {" ".join(shlex.quote(c) for c in cmd)}\n\n')
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, cwd=workdir,
                                stderr=subprocess.STDOUT, text=True, env=env)
        # watchdog: streaming line-by-line blocks until EOF, so a hung playbook
        # is killed out-of-band after JOB_TIMEOUT
        timed_out = {'v': False}
        def _kill():
            if proc.poll() is None:
                timed_out['v'] = True
                proc.kill()
        wd = threading.Timer(JOB_TIMEOUT, _kill)
        wd.start()
        try:
            for line in proc.stdout:
                log.write(line)
            proc.wait()
        finally:
            wd.cancel()
        if timed_out['v']:
            log.write(f'\n[ccp] playbook timed out after {JOB_TIMEOUT}s\n')
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
