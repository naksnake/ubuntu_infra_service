"""Automatic Slurm deployment: one job takes a cluster from "nodes assigned"
to "a batch job ran" — facts, hostnames, plan (controller + install method),
generate, deploy, validate, sbatch — and membership changes re-run it.
Everything remote is stubbed; the Ansible runner is stubbed to record the
playbook it would have run.
"""
import os, sys, time, json, tempfile, pathlib

TMP = tempfile.mkdtemp(prefix='ccp_slurmauto_')
os.environ.update({
    'CCP_DB': f'{TMP}/ccp.db',
    'CCP_FILES_DIR': f'{TMP}/files',
    'CCP_JOBS_DIR': f'{TMP}/jobs',
    'CCP_SSH_KEY': f'{TMP}/ssh/id_ccp',
    'CCP_ADMIN_USER': 'admin',
    'CCP_ADMIN_PASSWORD': 'adminpass123',
    'CCP_TEST_SYNC_JOBS': '1',
})
pathlib.Path(f'{TMP}/ssh').mkdir(parents=True)
pathlib.Path(f'{TMP}/ssh/id_ccp').write_text('FAKE\n')
pathlib.Path(f'{TMP}/ssh/id_ccp.pub').write_text('ssh-ed25519 AAAATEST ccp\n')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as ccp                                            # noqa: E402
import db                                                    # noqa: E402
import executor                                              # noqa: E402
import slurm                                                 # noqa: E402

ok = fail = 0

def check(name, cond, detail=''):
    global ok, fail
    if cond:
        ok += 1
        print(f'  PASS  {name}')
    else:
        fail += 1
        print(f'  FAIL  {name}  {detail}')

def client_for(username, password):
    c = ccp.app.test_client()
    r = c.post('/login', data={'username': username, 'password': password})
    assert r.status_code in (302, 303)
    with c.session_transaction() as s:
        csrf = s['csrf']
    return c, {'X-CSRF-Token': csrf}

def cluster(cid):
    return db.query('SELECT * FROM clusters WHERE id=?', (cid,), one=True)

def job(jid):
    return db.query('SELECT * FROM jobs WHERE id=?', (jid,), one=True)

admin, ah = client_for('admin', 'adminpass123')

# ── stubs: what each node answers ───────────────────────────────────────────
NODES = {}          # address -> node description used by the fake SSH

def facts_for(addr):
    n = NODES[addr]
    out = ['CCP_FACTS_BEGIN', f'hostname={n["hostname"]}', f'os_name={n["os_name"]}',
           'cpu_model=EPYC', 'cpu_sockets=2', 'threads_per_core=2',
           f'cpu_cores={n["cpus"]}', 'mem_kb=268435456']
    out += [f'gpu={g}' for g in n.get('gpus', [])]
    out += ['CCP_FACTS_END', 'CCP_PRE_BEGIN', 'os_id=ubuntu',
            f'os_version={n["os_version"]}', f'slurm_installed={n.get("installed", "")}']
    out += [f'slurm_avail={v}' for v in n.get('avail', [])]
    out += [f'nvidia_dev={d}' for d in n.get('devs', [])]
    out += ['cgroup=cgroup2fs', 'CCP_PRE_END']
    return '\n'.join(out) + '\n'

CALLS = []
SB = {'state': 'COMPLETED'}

def stub_key_ssh(address, user, port, command, timeout=None):
    CALLS.append((address, command))
    n = NODES.get(address)
    if n is not None and n.get('down'):
        return (255, f'ssh: connect to host {address} port 22: No route to host\n')
    if 'CCP_FACTS_BEGIN' in command:
        return (0, facts_for(address))
    if 'CCP_HOSTNAME_ACTUAL' in command:
        name = command.split('new=', 1)[1].split('\n', 1)[0].strip().strip("'")
        NODES[address]['hostname'] = name
        return (0, f'CCP_HOSTNAME_ACTUAL {name}\nCCP_HOSTNAME_OK {name}\n')
    if 'CCPEOF' in command:
        return (0, 'batch script staged\n')
    if 'sbatch --wait --parsable' in command:
        return (0, '77\n')
    if 'scontrol show job' in command:
        return (0, f"JobId=77 JobName=ccp-ai-smoke\n   JobState={SB['state']}\n"
                   "   BatchHost=rack0-sled1-cpu\n")
    if 'ccp-ai-smoke-77.out' in command:
        return (0, '[rack0-sled1-cpu] cpus=48\n[rack0-sled2-gpu] GPU 0: H100\n')
    if 'srun' in command:
        return (0, 'rack0-sled1-cpu\nrack0-sled2-gpu\n')
    return (0, 'stub-ok\n')

executor._key_ssh = stub_key_ssh
executor._password_ssh = lambda *a, **kw: (0, '')

ANSIBLE = {'rc': 0, 'runs': []}
def stub_ansible(job_id, spec, log):
    ANSIBLE['runs'].append(spec)
    log.write('PLAY [Deploy Slurm (stub)] ****\nTASK [stub] ****\nok: [all]\n')
    if ANSIBLE['rc']:
        log.write('TASK [Start slurmd on every node] ****\n'
                  'fatal: [rack0-sled2-gpu]: FAILED! => \n    changed: false\n    msg: |-\n'
                  '        slurmd failed to start on rack0-sled2-gpu. Its own reason:\n'
                  '        Sep 09 10:07:02 rack0-sled2-gpu slurmd[4711]: fatal: boom reason\n')
    log.write('PLAY RECAP ****\n')
    return ANSIBLE['rc']
executor._run_ansible = stub_ansible

# ── fixture: a GPU node whose box still has its factory hostname, a CPU node ─
cid = admin.post('/api/clusters', headers=ah,
                 json={'name': 'ai', 'kind': 'slurm'}).get_json()['id']

def mk(name, addr, cluster_id=cid, state='managed'):
    return db.execute(
        "INSERT INTO nodes (name,address,conn,ssh_user,ssh_port,groups,created_at,state,cluster_id) "
        "VALUES (?,?,'ssh','root',22,'',?,?,?)", (name, addr, int(time.time()), state, cluster_id))

NODES['10.0.3.1'] = {'hostname': 'rack0-sled1-cpu', 'os_name': 'Ubuntu 24.04.2 LTS',
                     'os_version': '24.04', 'cpus': 48, 'avail': ['23.11.4-1.2ubuntu5']}
NODES['10.0.3.2'] = {'hostname': 'gpu-node', 'os_name': 'Ubuntu 24.04.2 LTS',
                     'os_version': '24.04', 'cpus': 96, 'gpus': ['NVIDIA H100', 'NVIDIA H100'],
                     'devs': ['/dev/nvidia0', '/dev/nvidia1'], 'avail': ['23.11.4-1.2ubuntu5']}
cpu = mk('rack0-sled1-cpu', '10.0.3.1')
gpu = mk('rack0-sled2-gpu', '10.0.3.2')

print('== defaults ==')
c = cluster(cid)
check('a new Slurm cluster auto-deploys and decides the install method itself',
      c['auto_deploy'] == 1 and c['install_from'] == 'auto', dict(c))
check('parse_preflight reads the pre-flight section',
      executor.parse_preflight(facts_for('10.0.3.2')) == {
          'os_id': 'ubuntu', 'os_version': '24.04', 'slurm_installed': '',
          'slurm_avail': ['23.11.4-1.2ubuntu5'], 'nvidia_dev': ['/dev/nvidia0', '/dev/nvidia1'],
          'cgroup': 'cgroup2fs'}, executor.parse_preflight(facts_for('10.0.3.2')))
check('preflight script warms the driver, lists devices, versions and candidates',
      all(s in executor.PREFLIGHT_SCRIPT for s in
          ('nvidia-smi -L', 'ls /dev/nvidia[0-9]*', 'slurmd -V', 'apt-cache madison slurm-wlm',
           'VERSION_ID')))

print('== one click: same release → distro packages, CPU node becomes controller ==')
CALLS.clear()
r = admin.post(f'/api/clusters/{cid}/slurm/auto', headers=ah, json={})
check('accepted', r.status_code == 202, r.get_json())
jid = r.get_json()['job_id']
log = executor.job_log(jid)
check('job kind slurm_auto and succeeded', job(jid)['kind'] == 'slurm_auto'
      and job(jid)['status'] == 'success', dict(job(jid)))
order = [l for l in log.splitlines() if l.startswith('##STAGE## ')]
check('seven stages framed in order',
      [l.split()[1:3] for l in order] == [['1/7', 'facts'], ['2/7', 'hostnames'], ['3/7', 'plan'],
                                          ['4/7', 'generate'], ['5/7', 'deploy'],
                                          ['6/7', 'validate'], ['7/7', 'sbatch']], order)
check('every stage ended PASSED',
      log.count('##STAGE-END##') == 7 and log.count('PASSED') >= 9 and 'FAILED' not in log)
check('facts framed per node with device-file count',
      '===== rack0-sled2-gpu (10.0.3.2) : facts =====' in log
      and 'gpus=2 (device files now: 2)' in log
      and '===== rack0-sled1-cpu (10.0.3.1) : facts =====' in log, log)
hw = db.query('SELECT * FROM hardware WHERE node_id=?', (gpu,), one=True)
check('hardware saved from the fresh facts', hw and hw['gpu_count'] == 2 and hw['cpu_cores'] == 96)
check('GPU node hostname fixed (box said gpu-node), CPU node left alone',
      any(a == '10.0.3.2' and 'CCP_HOSTNAME_ACTUAL' in cmd for a, cmd in CALLS)
      and not any(a == '10.0.3.1' and 'CCP_HOSTNAME_ACTUAL' in cmd for a, cmd in CALLS)
      and 'hostname set and verified' in log, log)
check('controller auto-picked: the node without GPUs',
      'controller: rack0-sled1-cpu (auto — a node without GPUs' in log
      and cluster(cid)['controller_node_id'] == cpu, log)
check('install auto → distro packages pinned to the common version',
      'distro packages pinned to 23.11.4-1.2ubuntu5' in log, log)
c = cluster(cid)
check('configs generated and stored',
      'NodeName=rack0-sled2-gpu NodeAddr=10.0.3.2' in c['slurm_conf'] and 'Gres=gpu:2' in c['slurm_conf']
      and 'File=/dev/nvidia[0-1]' in c['gres_conf'], c['slurm_conf'])
check('deploy ran the pinned distro playbook without the long budget',
      len(ANSIBLE['runs']) == 1 and 'slurm-wlm=23.11.4-1.2ubuntu5' in ANSIBLE['runs'][0]['playbook']
      and 'timeout' not in ANSIBLE['runs'][0] and ANSIBLE['runs'][0]['node_ids'] == sorted([cpu, gpu]),
      {k: v for k, v in ANSIBLE['runs'][0].items() if k != 'playbook'})
check('validate + sbatch ran through the controller',
      'VALIDATE PASSED' in log and 'SBATCH PASSED' in log
      and any(a == '10.0.3.1' and 'sinfo' in cmd for a, cmd in CALLS))
check('AUTO DEPLOY PASSED and state VALIDATE',
      'AUTO DEPLOY PASSED' in log and cluster(cid)['slurm_state'] == 'VALIDATE', log)
check('audited', any('slurm.auto' == a['action'] for a in db.query('SELECT action FROM audit')))

print('== mixed Ubuntu releases → the same release is built from source ==')
NODES['10.0.3.1'].update({'os_version': '25.04', 'avail': ['25.11.2-1']})
r = admin.post(f'/api/clusters/{cid}/slurm/auto', headers=ah, json={})
log = executor.job_log(r.get_json()['job_id'])
check('plan: source build because no distro version is common',
      f'build slurm {slurm.SOURCE_DEFAULT_VERSION} from source' in log
      and 'no distro version exists on every node' in log, log)
check('deploy used the source playbook with an hour budget',
      ANSIBLE['runs'][-1].get('timeout') == 3600
      and f'Build and install slurm-{slurm.SOURCE_DEFAULT_VERSION}' in ANSIBLE['runs'][-1]['playbook'])
NODES['10.0.3.1'].update({'os_version': '24.04', 'avail': ['23.11.4-1.2ubuntu5']})

print('== a node already running a source build keeps source ==')
NODES['10.0.3.2']['installed'] = 'slurm 25.11.8'
r = admin.post(f'/api/clusters/{cid}/slurm/auto', headers=ah, json={})
log = executor.job_log(r.get_json()['job_id'])
check('plan: source, naming the node', 'rack0-sled2-gpu already runs a source-built Slurm' in log, log)
NODES['10.0.3.2']['installed'] = ''

print('== GPU declared but device files missing right now ==')
NODES['10.0.3.2']['devs'] = []
r = admin.post(f'/api/clusters/{cid}/slurm/auto', headers=ah, json={})
log = executor.job_log(r.get_json()['job_id'])
c = cluster(cid)
check('declared without GPUs, with a warning, and the run still completes',
      'WARNING: rack0-sled2-gpu: 2 GPU(s) recorded but only 0 /dev/nvidia*' in log
      and 'Gres=' not in c['slurm_conf'] and c['gres_conf'] == ''
      and 'AUTO DEPLOY PASSED' in log, log)
check('no GPU gate in that playbook (nothing declared)',
      'Probe GPU device files' not in ANSIBLE['runs'][-1]['playbook'])
NODES['10.0.3.2']['devs'] = ['/dev/nvidia0', '/dev/nvidia1']

print('== the first failing stage stops the run ==')
ANSIBLE['rc'] = 2
CALLS.clear()
r = admin.post(f'/api/clusters/{cid}/slurm/auto', headers=ah, json={})
jid = r.get_json()['job_id']
log = executor.job_log(jid)
check('deploy failure reported at its stage',
      '##STAGE-END## deploy FAILED' in log and 'AUTO DEPLOY FAILED at stage deploy' in log, log)
check('job failed, state stays DISCOVER, validation never attempted',
      job(jid)['status'] == 'failed' and cluster(cid)['slurm_state'] == 'DISCOVER'
      and not any('sinfo' in cmd for _, cmd in CALLS), dict(job(jid)))
tail = log.rsplit('AUTO DEPLOY FAILED', 1)[1]
check('the final failure line repeats the failed task with its reason (people copy the last block)',
      'reason (from the stage above):' in tail and 'fatal: [rack0-sled2-gpu]: FAILED!' in tail
      and 'fatal: boom reason' in tail, tail)
check('the excerpt is the fatal block only, not the whole playbook',
      'ok: [all]' not in tail and 'PLAY RECAP' not in tail, tail)
ANSIBLE['rc'] = 0

print('== an unreachable member stops before anything is changed ==')
NODES['10.0.3.1']['down'] = True
runs_before = len(ANSIBLE['runs'])
r = admin.post(f'/api/clusters/{cid}/slurm/auto', headers=ah, json={})
log = executor.job_log(r.get_json()['job_id'])
check('facts stage names the node and no playbook ran',
      'AUTO DEPLOY FAILED at stage facts: no facts from rack0-sled1-cpu' in log
      and '[rack0-sled1-cpu exit 255]' in log and len(ANSIBLE['runs']) == runs_before, log)
check('a stage without fatal blocks repeats its last lines as the reason',
      'No route to host' in log.rsplit('AUTO DEPLOY FAILED', 1)[1], log)
NODES['10.0.3.1']['down'] = False

print('== run_tests=false skips the sbatch stage ==')
r = admin.post(f'/api/clusters/{cid}/slurm/auto', headers=ah, json={'run_tests': False})
log = executor.job_log(r.get_json()['job_id'])
check('six stages, no sbatch, still passes',
      '##STAGE## 1/6 facts' in log and '##STAGE## 6/6 validate' in log
      and 'sbatch' not in ''.join(l for l in log.splitlines() if l.startswith('##STAGE##'))
      and 'AUTO DEPLOY PASSED' in log, log)

print('== one deployment at a time ==')
fake = db.execute("INSERT INTO jobs (kind,target,spec,status,created_by,created_at) "
                  "VALUES ('slurm_auto','ai',?,'running','admin',?)",
                  (json.dumps({'cluster_id': cid}), int(time.time())))
r = admin.post(f'/api/clusters/{cid}/slurm/auto', headers=ah, json={})
check('409 while a deployment runs, pointing at it',
      r.status_code == 409 and r.get_json()['job_id'] == fake, r.get_json())
check('membership change does not start a second one',
      executor.maybe_auto_deploy(cid, 'admin', 'test') is None)
db.execute("UPDATE jobs SET status='failed' WHERE id=?", (fake,))

print('== settings ==')
r = admin.patch(f'/api/clusters/{cid}', headers=ah,
                json={'install_from': 'source', 'slurm_version': '25.05.3',
                      'tarball_url': 'http://10.0.0.1/slurm-25.05.3.tar.bz2', 'auto_deploy': False})
c = cluster(cid)
check('saved', r.status_code == 200 and c['install_from'] == 'source' and c['slurm_version'] == '25.05.3'
      and c['tarball_url'] == 'http://10.0.0.1/slurm-25.05.3.tar.bz2' and c['auto_deploy'] == 0, dict(c))
r = admin.post(f'/api/clusters/{cid}/slurm/auto', headers=ah, json={})
log = executor.job_log(r.get_json()['job_id'])
check('the run follows the saved settings',
      'install: source 25.05.3 (cluster setting)' in log and 'tarball: http://10.0.0.1/slurm-25.05.3.tar.bz2' in log
      and 'Build and install slurm-25.05.3' in ANSIBLE['runs'][-1]['playbook']
      and 'http://10.0.0.1/slurm-25.05.3.tar.bz2' in ANSIBLE['runs'][-1]['playbook'], log)
for bad in ({'install_from': 'rpm'}, {'install_from': 'source', 'slurm_version': '23.11.4-1'},
            {'install_from': 'apt', 'tarball_url': 'ftp://mirror/x.tar.bz2'},
            {'controller_node_id': 999999}):
    r = admin.patch(f'/api/clusters/{cid}', headers=ah, json=bad)
    check(f'rejected: {bad}', r.status_code == 400, r.get_json())
r = admin.patch(f'/api/clusters/{cid}', headers=ah, json={'controller_node_id': gpu})
check('controller can be pinned', r.status_code == 200 and cluster(cid)['controller_node_id'] == gpu)
r = admin.patch(f'/api/clusters/{cid}', headers=ah, json={'install_from': 'auto', 'slurm_version': ''})
r = admin.post(f'/api/clusters/{cid}/slurm/auto', headers=ah, json={})
log = executor.job_log(r.get_json()['job_id'])
check('pinned controller kept', 'controller: rack0-sled2-gpu (kept from the cluster settings)' in log, log)
admin.patch(f'/api/clusters/{cid}', headers=ah, json={'controller_node_id': None, 'tarball_url': ''})

print('== membership changes re-run the pipeline when auto-deploy is on ==')
admin.patch(f'/api/clusters/{cid}', headers=ah, json={'auto_deploy': True})
NODES['10.0.3.3'] = {'hostname': 'rack1-sled1-cpu', 'os_name': 'Ubuntu 24.04.2 LTS',
                     'os_version': '24.04', 'cpus': 32, 'avail': ['23.11.4-1.2ubuntu5']}
n3 = mk('rack1-sled1-cpu', '10.0.3.3', cluster_id=None)
r = admin.post(f'/api/clusters/{cid}/nodes', headers=ah, json={'node_ids': [n3]})
d = r.get_json()
check('adding a node starts the pipeline and returns the job',
      r.status_code == 200 and d.get('job_id') and job(d['job_id'])['kind'] == 'slurm_auto', d)
log = executor.job_log(d['job_id'])
check('the run covers the new member and says why it ran',
      '3 node(s) — members added' in log and 'NodeName=rack1-sled1-cpu' in log
      and 'AUTO DEPLOY PASSED' in log, log)
r = admin.delete(f'/api/clusters/{cid}/nodes/{n3}', headers=ah)
d = r.get_json()
check('removing a node re-runs it too', d.get('job_id') and 'members removed' in executor.job_log(d['job_id']), d)
admin.patch(f'/api/clusters/{cid}', headers=ah, json={'auto_deploy': False})
jobs_before = db.query('SELECT COUNT(*) AS c FROM jobs', one=True)['c']
r = admin.post(f'/api/clusters/{cid}/nodes', headers=ah, json={'node_ids': [n3]})
check('auto-deploy off → assignment only',
      r.get_json().get('job_id') is None
      and db.query('SELECT COUNT(*) AS c FROM jobs', one=True)['c'] == jobs_before)
gcid = admin.post('/api/clusters', headers=ah, json={'name': 'plain', 'kind': 'generic'}).get_json()['id']
r = admin.post(f'/api/clusters/{gcid}/nodes', headers=ah, json={'node_ids': [n3]})
check('generic clusters never deploy anything', r.get_json().get('job_id') is None)

print('== onboarding into a Slurm cluster joins it automatically ==')
admin.patch(f'/api/clusters/{cid}', headers=ah, json={'auto_deploy': True})
NODES['10.0.3.4'] = {'hostname': 'rack1-sled2-cpu', 'os_name': 'Ubuntu 24.04.2 LTS',
                     'os_version': '24.04', 'cpus': 32, 'avail': ['23.11.4-1.2ubuntu5']}
pre = mk('rack1-sled2-cpu', '10.0.3.4', state='discovered')      # imported, not yet onboarded
executor._key_ssh = lambda a, u, p, cmd, timeout=None: ((0, 'CCP_OK\nrack1-sled2-cpu\n')
                                                        if 'CCP_OK' in cmd else stub_key_ssh(a, u, p, cmd))
jobs_before = db.query('SELECT COUNT(*) AS c FROM jobs', one=True)['c']
r = admin.post(f'/api/nodes/{pre}/onboard', headers=ah,
               json={'username': 'root', 'password': 'pw', 'set_hostname': False})
check('onboarding accepted', r.status_code == 202, r.get_json())
auto_jobs = db.query("SELECT * FROM jobs WHERE kind='slurm_auto' ORDER BY id DESC LIMIT 1", one=True)
check('node became managed', db.query('SELECT state FROM nodes WHERE id=?', (pre,), one=True)['state'] == 'managed',
      (r.status_code, r.get_json()))
check('its cluster started the pipeline by itself',
      auto_jobs and 'became managed' in executor.job_log(auto_jobs['id'])
      and 'rack1-sled2-cpu' in executor.job_log(auto_jobs['id']),
      executor.job_log(auto_jobs['id'])[-400:] if auto_jobs else None)
executor._key_ssh = stub_key_ssh

print('== the Clusters page ==')
r = admin.get('/clusters')
body = r.get_data(as_text=True)
check('one-click button, auto-deploy toggle, last run and the advanced panel render',
      r.status_code == 200 and 'Deploy Slurm automatically' in body
      and 'auto-deploy when members change' in body and 'last run:' in body
      and 'Settings &amp; manual steps' in body and 'install: auto' in body, r.status_code)

print('== RBAC ==')
admin.post('/api/users', headers=ah, json={'username': 'v9', 'password': 'password123', 'role': 'viewer'})
viewer, vh = client_for('v9', 'password123')
check('viewer cannot start the pipeline',
      viewer.post(f'/api/clusters/{cid}/slurm/auto', headers=vh, json={}).status_code == 403)
check('viewer cannot change settings',
      viewer.patch(f'/api/clusters/{cid}', headers=vh, json={'auto_deploy': False}).status_code == 403)

print(f'\n{ok} passed, {fail} failed')
sys.exit(1 if fail else 0)
