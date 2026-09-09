"""Slurm lifecycle tests: stage guards, discover fan-out, validate/sbatch/
benchmark/monitor over stubbed SSH (asserting node-to-node — never loopback —
testing, and a real sbatch → scontrol → output round trip), report
aggregation, cleanup, and state advancement.
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

# SSH stub that records every (address, command) pair (and the per-call
# timeout, which the batch test must raise above the onboarding default)
CALLS = []
TIMEOUTS = []
FACTS = ('CCP_FACTS_BEGIN\nos_name=Ubuntu 24.04\ncpu_model=EPYC\ncpu_cores=64\n'
         'mem_kb=131072000\ngpu=NVIDIA H100\nCCP_FACTS_END\n')
# what the stubbed scheduler reports for the batch job
SB = {'jid': '4242', 'state': 'COMPLETED', 'batch_host': 'rack0_sled2_gpu',
      'submit_rc': 0, 'submit_out': None}
SB_OUT = ('== CCP AI-training smoke test: job 4242 on 2 node(s): rack0_sled[1-2]_gpu ==\n'
          '[rack0_sled1_gpu] cpus=64\n[rack0_sled1_gpu] GPU 0: NVIDIA H100 (UUID: GPU-1)\n'
          '[rack0_sled2_gpu] cpus=64\n[rack0_sled2_gpu] GPU 0: NVIDIA H100 (UUID: GPU-2)\n'
          '[rack0_sled1_gpu] training step (numpy matmul 2048x2048): 0.412s\n'
          '[rack0_sled2_gpu] training step (numpy matmul 2048x2048): 0.398s\n'
          '== CCP AI-training smoke test finished ==\n')

def stub_key_ssh(address, user, port, command, timeout=None):
    CALLS.append((address, command))
    TIMEOUTS.append((command, timeout))
    if 'CCP_FACTS_BEGIN' in command:
        return (0, FACTS)
    if 'CCPEOF' in command:                       # staging the batch script
        return (0, 'batch script staged\n')
    if 'sbatch --wait --parsable' in command:
        out = SB['submit_out'] if SB['submit_out'] is not None else SB['jid'] + '\n'
        return (SB['submit_rc'], out)
    if 'scontrol show job' in command:
        return (0, f"JobId={SB['jid']} JobName=ccp-ai-smoke\n   JobState={SB['state']} "
                   f"Reason=None ExitCode=0:0\n   BatchHost={SB['batch_host']}\n")
    if f"cat /tmp/ccp-ai-smoke-{SB['jid']}.out" in command:
        return (0, SB_OUT)
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
r = admin.post(f'/api/clusters/{cid}/slurm/action', headers=ah, json={'stage': 'sbatch'})
check('sbatch test before deploy → 400', r.status_code == 400, r.get_json())
r = admin.post(f'/api/clusters/{cid}/slurm/action', headers=ah, json={'stage': 'cleanup'})
check('cleanup is always allowed — it is the recovery path for a node left '
      'with slurmd enabled but no config', r.status_code == 202, r.get_json())

print('== collect logs (diagnose) works in any state and changes nothing ==')
CALLS.clear()
r = admin.post(f'/api/clusters/{cid}/slurm/action', headers=ah, json={'stage': 'diagnose'})
check('diagnose allowed before deploy (a failed deploy is exactly when it is needed)',
      r.status_code == 202, r.get_json())
jid = r.get_json()['job_id']
log = executor.job_log(jid)
diag = [(a, c) for a, c in CALLS if 'CCPDIAG' in c]
check('bundle runs on every member', sorted(a for a, _ in diag) == ['10.0.2.1', '10.0.2.2'], CALLS)
check('bundle gathers journal, unit + drop-ins, configs, hardware view and ports',
      all('journalctl -u slurmd -n 60' in c and 'systemctl cat slurmd' in c
          and '/etc/slurm/slurm.conf' in c and 'slurmd -C' in c and '6817|6818' in c
          for _, c in diag), diag)
check('foreground probe uses each node\'s own NodeName',
      any('slurmd -D -vv -N rack0_sled1_gpu' in c for a, c in diag if a == '10.0.2.1')
      and any('slurmd -D -vv -N rack0_sled2_gpu' in c for a, c in diag if a == '10.0.2.2'), diag)
check('bundle works for a non-root ssh user (sudo -n prefix)', all('sudo -n' in c for _, c in diag))
check('no controller yet → no controller-only checks', not any('scontrol ping' in c for _, c in diag))
check('one frame per node in the log',
      '===== rack0_sled1_gpu (10.0.2.1) : diagnostics bundle =====' in log
      and '===== rack0_sled2_gpu (10.0.2.2) : diagnostics bundle =====' in log, log)
check('marker tells the operator what to do with it', 'DIAGNOSTICS COLLECTED' in log and 'Download log' in log)
check('diagnose never advances the lifecycle', cluster_state(cid) == 'INIT')
job = db.query('SELECT * FROM jobs WHERE id=?', (jid,), one=True)
check('collection job succeeds when every node answered', job['status'] == 'success', dict(job))
r = admin.get(f'/api/jobs/{jid}/log')
check('raw log downloadable as a text attachment',
      r.status_code == 200 and r.mimetype == 'text/plain'
      and 'attachment' in r.headers.get('Content-Disposition', '')
      and f'ccp-job-{jid}-slurm_action.log' in r.headers['Content-Disposition']
      and b'DIAGNOSTICS COLLECTED' in r.data, (r.status_code, dict(r.headers)))
r = admin.get('/api/jobs/999999/log')
check('unknown job log → 404', r.status_code == 404)

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

print('== diagnose with a controller adds the controller-only checks ==')
CALLS.clear()
r = admin.post(f'/api/clusters/{cid}/slurm/action', headers=ah, json={'stage': 'diagnose'})
diag = [(a, c) for a, c in CALLS if 'CCPDIAG' in c]
ctl_cmd = next(c for a, c in diag if a == '10.0.2.1')
oth_cmd = next(c for a, c in diag if a == '10.0.2.2')
check('controller bundle adds scontrol ping / sinfo / slurmctld log and probe',
      'scontrol ping' in ctl_cmd and 'sinfo -N -l' in ctl_cmd and 'slurmctld.log' in ctl_cmd
      and 'runuser -u slurm -- timeout 8 slurmctld -D -vv' in ctl_cmd, ctl_cmd)
check('member bundle has none of them', 'scontrol ping' not in oth_cmd and 'runuser' not in oth_cmd)
check('controller frame labelled', '(controller)' in executor.job_log(r.get_json()['job_id']))
check('state untouched by diagnose', cluster_state(cid) == 'VALIDATE')

print('== sbatch: an AI-training-shaped batch job through the real scheduler ==')
CALLS.clear(); TIMEOUTS.clear()
db.execute("UPDATE clusters SET slurm_state='DEPLOY' WHERE id=?", (cid,))
r = admin.post(f'/api/clusters/{cid}/slurm/action', headers=ah, json={'stage': 'sbatch'})
check('sbatch test accepted once deployed', r.status_code == 202, r.get_json())
jid = r.get_json()['job_id']
log = executor.job_log(jid)
stage_cmd = next((c for a, c in CALLS if 'CCPEOF' in c and a == '10.0.2.1'), '')
check('batch script staged on the controller via a quoted heredoc',
      stage_cmd.startswith("cat > /tmp/ccp-ai-smoke.sh <<'CCPEOF'"), stage_cmd[:80])
check('script asks for one task on every member node',
      '#SBATCH --nodes=2' in stage_cmd and '#SBATCH --ntasks-per-node=1' in stage_cmd
      and '#SBATCH --job-name=ccp-ai-smoke' in stage_cmd, stage_cmd)
check('script inventories GPUs and times a training step on every node',
      'nvidia-smi -L' in stage_cmd and 'srun python3' in stage_cmd
      and 'training step' in stage_cmd and 'import torch' in stage_cmd, stage_cmd)
gres_conf = db.query('SELECT gres_conf FROM clusters WHERE id=?', (cid,), one=True)['gres_conf'] or ''
expect_gres = all(f'NodeName={n}' in gres_conf for n in ('rack0_sled1_gpu', 'rack0_sled2_gpu'))
check('GPU reservation follows gres.conf (only when every member declares one)',
      ('#SBATCH --gres=gpu:1' in stage_cmd) == expect_gres, (expect_gres, gres_conf))
check('submitted with sbatch --wait --parsable on the controller',
      any(a == '10.0.2.1' and c == 'sbatch --wait --parsable /tmp/ccp-ai-smoke.sh'
          for a, c in CALLS), CALLS)
check('the wait uses the batch budget, not the 60 s onboarding step timeout',
      any('sbatch --wait' in c and t == executor.SBATCH_WAIT_SECONDS for c, t in TIMEOUTS)
      and executor.SBATCH_WAIT_SECONDS > executor.ONBOARD_STEP_TIMEOUT, TIMEOUTS)
check('job state read back with scontrol',
      any('scontrol show job 4242' in c for _, c in CALLS))
check('output fetched from the batch host (sled2), not blindly from the controller',
      any(a == '10.0.2.2' and c == 'cat /tmp/ccp-ai-smoke-4242.out' for a, c in CALLS), CALLS)
check('per-host framing so the console groups it by node',
      '===== rack0_sled1_gpu (10.0.2.1) : sbatch --wait' in log
      and '===== rack0_sled2_gpu (10.0.2.2) : job 4242 output (batch host) =====' in log
      and '[rack0_sled2_gpu exit 0]' in log, log)
check('job output (GPU list + training step timings) lands in the log',
      'NVIDIA H100 (UUID: GPU-2)' in log and 'training step (numpy matmul' in log, log)
check('PASSED marker', 'SBATCH PASSED' in log, log)
check('a passing batch job is the functional validation → state VALIDATE',
      cluster_state(cid) == 'VALIDATE')

# scheduler ran it but the job failed (e.g. a node killed the step)
db.execute("UPDATE clusters SET slurm_state='DEPLOY' WHERE id=?", (cid,))
SB['state'] = 'FAILED'
r = admin.post(f'/api/clusters/{cid}/slurm/action', headers=ah, json={'stage': 'sbatch'})
log = executor.job_log(r.get_json()['job_id'])
check('JobState other than COMPLETED fails the stage',
      'SBATCH FAILED (JobState=FAILED' in log, log)
check('failed batch job does not advance', cluster_state(cid) == 'DEPLOY')
# controller unreachable / partition down: sbatch returns no id
SB['state'] = 'COMPLETED'
SB['submit_rc'], SB['submit_out'] = 1, 'sbatch: error: Batch job submission failed: Required node not available (down, drained or reserved)\n'
r = admin.post(f'/api/clusters/{cid}/slurm/action', headers=ah, json={'stage': 'sbatch'})
log = executor.job_log(r.get_json()['job_id'])
check('submission failure is reported with a hint, no scontrol/cat attempted',
      'SBATCH FAILED: sbatch returned no job id' in log and 'run Validate' in log
      and 'Required node not available' in log
      and not any('scontrol show job' in c for _, c in CALLS[-3:]), log)
check('state unchanged after a failed submission', cluster_state(cid) == 'DEPLOY')
SB['submit_rc'], SB['submit_out'] = 0, None
db.execute("UPDATE clusters SET slurm_state='VALIDATE' WHERE id=?", (cid,))

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
executor._key_ssh = lambda a, u, p, cmd, **kw: (1, 'slurm_load_partitions: unable to contact controller\n')
r = admin.post(f'/api/clusters/{cid}/slurm/action', headers=ah, json={'stage': 'validate'})
jid = r.get_json()['job_id']
check('failed validate keeps state', cluster_state(cid) == 'DEPLOY')
check('failure visible in log', 'VALIDATE FAILED' in executor.job_log(jid))

print(f'\n{ok} passed, {fail} failed')
sys.exit(1 if fail else 0)
