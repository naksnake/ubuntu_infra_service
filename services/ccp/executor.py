"""Job execution: parallel shell over ClusterShell and Ansible playbooks.

Jobs run in a background daemon thread and stream their output to a per-job log
file under JOBS_DIR; the jobs table (status/exit_code) is the source of truth so
the UI can poll regardless of which worker/thread produced the job. Nodes marked
conn='local' run directly via subprocess so the panel is demoable without any
reachable SSH hosts; conn='ssh' nodes are executed in parallel through
ClusterShell (shell jobs) or ansible-playbook (playbook jobs).
"""
import os
import json
import time
import shlex
import tempfile
import threading
import subprocess

import db

JOBS_DIR = os.environ.get('CCP_JOBS_DIR', '/data/ccp/jobs')
SSH_KEY = os.environ.get('CCP_SSH_KEY', '/data/ccp/ssh/id_ccp')
SSH_COMMON = ['-o', 'StrictHostKeyChecking=no',
              '-o', 'UserKnownHostsFile=/dev/null',
              '-o', 'ConnectTimeout=10',
              '-o', 'BatchMode=yes']


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


def start_job(kind, target, spec, created_by):
    """Insert a job row and kick off its background thread. Returns the job id."""
    job_id = db.execute(
        'INSERT INTO jobs (kind, target, spec, status, created_by, created_at) '
        'VALUES (?,?,?,?,?,?)',
        (kind, target, json.dumps(spec), 'running', created_by, int(time.time())))
    open(_log_path(job_id), 'w').close()
    t = threading.Thread(target=_run, args=(job_id, kind, spec), daemon=True)
    t.start()
    return job_id


def _run(job_id, kind, spec):
    log = open(_log_path(job_id), 'a', buffering=1)
    try:
        if kind == 'shell':
            rc = _run_shell(job_id, spec, log)
        elif kind == 'ansible':
            rc = _run_ansible(job_id, spec, log)
        else:
            log.write(f'unknown job kind: {kind}\n')
            rc = 2
        _finish(job_id, 'success' if rc == 0 else 'failed', rc)
    except Exception as exc:  # never let a job thread die silently
        log.write(f'\n[ccp] job crashed: {exc}\n')
        _finish(job_id, 'failed', 1)
    finally:
        log.close()


# ── shell / ClusterShell ─────────────────────────────────────────────────────

def _run_shell(job_id, spec, log):
    nodes = _resolve_nodes(spec.get('node_ids', []))
    command = spec.get('command', '')
    if not nodes:
        log.write('[ccp] no target nodes resolved\n')
        return 2

    worst = 0
    local = [n for n in nodes if n['conn'] == 'local']
    remote = [n for n in nodes if n['conn'] != 'local']

    for n in local:
        log.write(f'===== {n["name"]} ({n["address"]}, local) =====\n')
        p = subprocess.run(['/bin/sh', '-c', command],
                           capture_output=True, text=True)
        log.write(p.stdout)
        if p.stderr:
            log.write(p.stderr)
        log.write(f'[exit {p.returncode}]\n\n')
        worst = max(worst, p.returncode)

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
        task.run(command, nodes=NodeSet.fromlist(addrs))

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


# ── ansible ──────────────────────────────────────────────────────────────────

def _run_ansible(job_id, spec, log):
    nodes = _resolve_nodes(spec.get('node_ids', []))
    playbook = spec.get('playbook', '')
    extra_vars = spec.get('extra_vars', '')
    if not nodes:
        log.write('[ccp] no target nodes resolved\n')
        return 2
    if not playbook.strip():
        log.write('[ccp] empty playbook\n')
        return 2

    with tempfile.TemporaryDirectory() as tmp:
        inv_path = os.path.join(tmp, 'inventory.ini')
        pb_path = os.path.join(tmp, 'playbook.yml')
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
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, env=env)
        for line in proc.stdout:
            log.write(line)
        proc.wait()
        return proc.returncode


# ── helpers ──────────────────────────────────────────────────────────────────

def _resolve_nodes(node_ids):
    if not node_ids:
        return []
    marks = ','.join('?' for _ in node_ids)
    rows = db.query(f'SELECT * FROM nodes WHERE id IN ({marks})', tuple(node_ids))
    return [dict(r) for r in rows]
