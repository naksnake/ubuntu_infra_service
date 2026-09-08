"""Slurm lifecycle tests: stage guards, discover fan-out, validate/benchmark/
monitor over stubbed SSH (asserting node-to-node — never loopback — testing),
report aggregation, cleanup, and state advancement.
"""
import os, sys, time, tempfile, pathlib

TMP = tempfile.mkdtemp(prefix='ccp_slurmlc_')
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

def cluster_state(cid):
    return db.query('SELECT slurm_state FROM clusters WHERE id=?',
                    (cid,), one=True)['slurm_state']

admin, ah = client_for('admin', 'adminpass123')

# SSH stub that records every (address, command) pair
CALLS = []
FACTS = ('CCP_FACTS_BEGIN\nos_name=Ubuntu 24.04\ncpu_model=EPYC\ncpu_cores=64\n'
         'mem_kb=131072000\ngpu=NVIDIA H100\nCCP_FACTS_END\n')

def stub_key_ssh(address, user, port, command):
    CALLS.append((address, command))
    if 'CCP_FACTS_BEGIN' in command:
        return (0, FACTS)
    if 'iperf3 -s' in command or 'command -v iperf3' in command:
        return (0, 'IPERF_SERVER_READY\n')
    if 'iperf3 -c' in command:
        return (0, '[  5]   0.00-5.00   sec  5.5 GBytes  9414 Mbits/sec\n')
    if 'srun' in command:
        return (0, 'rack0_sled1_gpu\nrack0_sled2_gpu\n')
    return (0, 'stub-ok\n')

executor._key_ssh = stub_key_ssh
executor._password_ssh = lambda *a, **kw: (0, '')

cid = admin.post('/api/clusters', headers=ah,
                 json={'name': 'ai', 'kind': 'slurm'}).get_json()['id']
n1 = db.execute("INSERT INTO nodes (name,address,conn,ssh_user,ssh_port,groups,created_at,state,cluster_id) "
                "VALUES ('rack0_sled1_gpu','10.0.2.1','ssh','root',22,'',?,'managed',?)",
                (int(time.time()), cid))
n2 = db.execute("INSERT INTO nodes (name,address,conn,ssh_user,ssh_port,groups,created_at,state,cluster_id) "
                "VALUES ('rack0_sled2_gpu','10.0.2.2','ssh','root',22,'',?,'managed',?)",
                (int(time.time()), cid))

print('== guards ==')
r = admin.post(f'/api/clusters/{cid}/slurm/action', headers=ah, json={'stage': 'bogus'})
check('unknown stage → 400', r.status_code == 400)
r = admin.post(f'/api/clusters/{cid}/slurm/action', headers=ah, json={'stage': 'validate'})
check('validate before deploy → 400', r.status_code == 400, r.get_json())
r = admin.post(f'/api/clusters/{cid}/slurm/action', headers=ah, json={'stage': 'cleanup'})
check('cleanup before deploy → 400', r.status_code == 400)

print('== discover fans out hwscans and advances ==')
r = admin.post(f'/api/clusters/{cid}/slurm/action', headers=ah, json={'stage': 'discover'})
d = r.get_json()
check('discover queues one hwscan per member',
      r.status_code == 202 and len(d['job_ids']) == 2, d)
check('state → DISCOVER', cluster_state(cid) == 'DISCOVER')
hw = db.query('SELECT * FROM hardware WHERE node_id=?', (n1,), one=True)
check('facts collected via stub', hw and hw['cpu_cores'] == 64, dict(hw) if hw else None)

print('== validate/benchmark need the deployed state ==')
admin.post(f'/api/clusters/{cid}/slurm/generate', headers=ah,
           json={'controller_node_id': n1})
db.execute("UPDATE clusters SET slurm_state='DEPLOY' WHERE id=?", (cid,))

CALLS.clear()
r = admin.post(f'/api/clusters/{cid}/slurm/action', headers=ah, json={'stage': 'validate'})
check('validate accepted', r.status_code == 202, r.get_json())
jid = r.get_json()['job_id']
log = executor.job_log(jid)
check('validate runs sinfo + srun across all nodes on the controller',
      any('sinfo' in c for _, c in CALLS)
      and any('srun -N 2' in c for _, c in CALLS)
      and all(a == '10.0.2.1' for a, _ in CALLS), CALLS)
check('state → VALIDATE', cluster_state(cid) == 'VALIDATE')
check('job succeeded with PASSED marker', 'VALIDATE PASSED' in log)

print('== benchmark is node-to-node, never loopback ==')
CALLS.clear()
r = admin.post(f'/api/clusters/{cid}/slurm/action', headers=ah, json={'stage': 'benchmark'})
jid = r.get_json()['job_id']
log = executor.job_log(jid)
check('timed srun dispatch across all nodes', any('time -p srun -N 2' in c for _, c in CALLS))
check('controller pings the other member (node-to-node RTT)',
      any(a == '10.0.2.1' and 'ping -c 3' in c and '10.0.2.2' in c for a, c in CALLS), CALLS)
iperf_server = [(a, c) for a, c in CALLS if 'iperf3 -s' in c]
iperf_client = [(a, c) for a, c in CALLS if 'iperf3 -c' in c]
check('iperf3 server and client run on two different nodes',
      iperf_server and iperf_client and iperf_server[0][0] != iperf_client[0][0],
      (iperf_server, iperf_client))
check('iperf3 client targets the server node address (not localhost)',
      '10.0.2.1' in iperf_client[0][1] and iperf_client[0][0] == '10.0.2.2')
check('bandwidth result in log', '9414 Mbits/sec' in log)
check('state → BENCHMARK', cluster_state(cid) == 'BENCHMARK')

print('== report aggregates locally ==')
CALLS.clear()
r = admin.post(f'/api/clusters/{cid}/slurm/action', headers=ah, json={'stage': 'report'})
jid = r.get_json()['job_id']
log = executor.job_log(jid)
check('report needs no SSH', CALLS == [])
check('report lists members, capacity and history',
      'rack0_sled1_gpu [controller]' in log and '128 CPUs' in log
      and 'NVIDIA H100' in log and 'benchmark: success' in log, log)
check('state → REPORT', cluster_state(cid) == 'REPORT')

print('== monitor ==')
r = admin.post(f'/api/clusters/{cid}/slurm/action', headers=ah, json={'stage': 'monitor'})
log = executor.job_log(r.get_json()['job_id'])
check('monitor snapshots sinfo + squeue', 'squeue' in log and 'sinfo' in log)
check('state → MONITOR', cluster_state(cid) == 'MONITOR')

print('== cleanup ==')
r = admin.post(f'/api/clusters/{cid}/slurm/action', headers=ah, json={'stage': 'cleanup'})
check('cleanup accepted once deployed', r.status_code == 202, r.get_json())
job = db.query('SELECT * FROM jobs ORDER BY id DESC LIMIT 1', one=True)
check('cleanup is a teardown playbook job', job['kind'] == 'slurm_deploy'
      and 'Slurm cleanup' in job['spec'])
# ansible-playbook missing in the test env → job fails → state unchanged
check('failed cleanup does not advance', cluster_state(cid) == 'MONITOR')

print('== failing stage does not advance ==')
db.execute("UPDATE clusters SET slurm_state='DEPLOY' WHERE id=?", (cid,))
executor._key_ssh = lambda a, u, p, cmd: (1, 'slurm_load_partitions: unable to contact controller\n')
r = admin.post(f'/api/clusters/{cid}/slurm/action', headers=ah, json={'stage': 'validate'})
jid = r.get_json()['job_id']
check('failed validate keeps state', cluster_state(cid) == 'DEPLOY')
check('failure visible in log', 'VALIDATE FAILED' in executor.job_log(jid))

print(f'\n{ok} passed, {fail} failed')
sys.exit(1 if fail else 0)
