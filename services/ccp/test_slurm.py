"""Slurm builder tests: slurm.conf/gres.conf generation from hardware
fixtures, the generate/deploy API (guards, storage, playbook contents), and
lifecycle advancement on successful deploy.
"""
import os, sys, time, json, tempfile, pathlib

TMP = tempfile.mkdtemp(prefix='ccp_slurm_')
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

print('== pure generation ==')
members = [
    {'id': 1, 'name': 'rack0_sled1_gpu', 'address': '192.168.100.21'},
    {'id': 2, 'name': 'rack0_sled2_gpu', 'address': '192.168.100.22'},
    {'id': 3, 'name': 'rack0_sled3_cpu', 'address': '192.168.100.23'},
]
hw = {
    1: {'cpu_cores': 128, 'cpu_sockets': 2, 'threads_per_core': 2,
        'mem_mb': 515899, 'gpu_count': 4},
    2: {'cpu_cores': 128, 'cpu_sockets': 2, 'threads_per_core': 2,
        'mem_mb': 515899, 'gpu_count': 1},
    3: {'cpu_cores': 7, 'cpu_sockets': 1, 'threads_per_core': 2,   # 7 % 2 != 0
        'mem_mb': 15923, 'gpu_count': 0},
}
conf, gres, warnings = slurm.generate('ai-train', members, members[0], hw)
check('controller line with address',
      'SlurmctldHost=rack0_sled1_gpu(192.168.100.21)' in conf, conf)
check('gpu node line: CPUs/geometry/memory/gres',
      'NodeName=rack0_sled1_gpu NodeAddr=192.168.100.21 CPUs=128 Sockets=2 '
      'CoresPerSocket=32 ThreadsPerCore=2 RealMemory=515387 Gres=gpu:4 '
      'State=UNKNOWN' in conf, conf)
check('inconsistent geometry omitted, CPUs kept',
      'NodeName=rack0_sled3_cpu NodeAddr=192.168.100.23 CPUs=7 RealMemory=15411 '
      'State=UNKNOWN' in conf, conf)
check('GresTypes only when GPUs exist', 'GresTypes=gpu' in conf)
check('partition covers all members',
      'PartitionName=main Nodes=rack0_sled1_gpu,rack0_sled2_gpu,rack0_sled3_cpu '
      'Default=YES MaxTime=INFINITE State=UP' in conf, conf)
check('no warnings when facts exist', warnings == [])
check('gres multi-gpu range + single-gpu file',
      'NodeName=rack0_sled1_gpu Name=gpu File=/dev/nvidia[0-3]' in gres
      and 'NodeName=rack0_sled2_gpu Name=gpu File=/dev/nvidia0' in gres, gres)
check('cpu-only node absent from gres', 'sled3' not in gres)

print('== GPU declaration depends on the driver being loaded ==')
# verified against real slurm 23.11.4: declaring a GPU whose /dev/nvidia*
# does not exist makes slurmd hang on "Waiting for gres.conf file /dev/nvidia0"
gpu_pci = [{'id': 9, 'name': 'gpu-nodrv', 'address': '10.0.0.9'}]
hw_pci = {9: {'cpu_cores': 8, 'mem_mb': 16000, 'gpu_count': 2,
              'raw_json': json.dumps({'pci_gpu': ['03:00.0 VGA: NVIDIA H100']})}}
c_pci, g_pci, w_pci = slurm.generate('x', gpu_pci, gpu_pci[0], hw_pci)
check('driverless GPU (lspci only) is NOT declared',
      'Gres=gpu' not in c_pci and 'GresTypes' not in c_pci and g_pci == '',
      (c_pci, g_pci))
check('driverless GPU warns with the reason and the remedy',
      w_pci and 'driver is not loaded' in w_pci[0] and 'rescan' in w_pci[0], w_pci)

hw_nvml = {9: {'cpu_cores': 8, 'mem_mb': 16000, 'gpu_count': 2,
               'raw_json': json.dumps({'gpu': ['NVIDIA H100', 'NVIDIA H100']})}}
c_nv, g_nv, w_nv = slurm.generate('x', gpu_pci, gpu_pci[0], hw_nvml)
check('driver-detected GPU IS declared with device files',
      'Gres=gpu:2' in c_nv and 'GresTypes=gpu' in c_nv
      and 'File=/dev/nvidia[0-1]' in g_nv, (c_nv, g_nv))
check('driver-detected GPU raises no driver warning',
      not any('driver is not loaded' in w for w in w_nv), w_nv)

hw_bare = {9: {'cpu_cores': 8, 'mem_mb': 16000, 'gpu_count': 2}}  # no raw_json
c_b, g_b, _ = slurm.generate('x', gpu_pci, gpu_pci[0], hw_bare)
check('summary-only hardware still declares GPUs (back-compat)',
      'Gres=gpu:2' in c_b and 'File=/dev/nvidia[0-1]' in g_b)

conf2, gres2, warn2 = slurm.generate('cpu-only', [members[2]], members[2],
                                     {3: hw[3]})
check('no-GPU cluster: empty gres, no GresTypes',
      gres2 == '' and 'GresTypes' not in conf2)

conf3, _, warn3 = slurm.generate('bare', [members[1]], members[1], {})
check('missing hardware → warning + minimal defaults',
      warn3 and 'CPUs=1 RealMemory=256' in conf3, (warn3, conf3))

print('== deploy playbook ==')
pb = slurm.deploy_playbook(conf, gres, 'rack0_sled1_gpu')
check('playbook inlines slurm.conf', 'ClusterName=ai-train' in pb)
check('SlurmctldHost name resolved to the real hostname at deploy time, '
      'address kept',
      "SlurmctldHost={{ hostvars[slurm_controller]['ansible_hostname'] }}"
      '(192.168.100.21)' in pb
      and 'SlurmctldHost=rack0_sled1_gpu' not in pb, pb)
check('stored conf keeps the inventory-name intent',
      'SlurmctldHost=rack0_sled1_gpu(192.168.100.21)' in conf)
check('MailProg pinned so minimal installs do not log errors',
      'MailProg=/bin/true' in conf)
check('playbook inlines gres.conf', '/dev/nvidia[0-3]' in pb)
check('munge key generated on controller and distributed',
      'mungekey' in pb and 'slurp' in pb and 'b64decode' in pb)
check('controller-only slurmctld', 'slurmctld' in pb
      and 'inventory_hostname == slurm_controller' in pb)
check('slurmctld disabled on non-controllers (no failed-everywhere noise)',
      'Disable slurmctld on non-controller nodes' in pb
      and 'inventory_hostname != slurm_controller' in pb, pb)
check('facts gathered for the SlurmctldHost resolution',
      'gather_facts: true' in pb)
check('slurmd NodeName pinned via -N (independent of OS hostname)',
      'SLURMD_OPTIONS=-N {{ inventory_hostname }}' in pb
      and 'slurmd.service.d' in pb, pb)
check('slurmd start does a daemon-reload so the drop-in is read',
      'daemon_reload: true' in pb)
import yaml as _yaml
_docs = list(_yaml.safe_load_all(pb))
check('deploy playbook is valid YAML', _docs and isinstance(_docs[0], list), type(_docs[0]))
cleanup = slurm.cleanup_playbook()
check('cleanup stops services and removes configs',
      'slurmd' in cleanup and '/etc/slurm/slurm.conf' in cleanup)

print('== API ==')
admin, ah = client_for('admin', 'adminpass123')
executor._password_ssh = lambda *a, **kw: (0, '')
executor._key_ssh = lambda a, u, p, cmd: (0, 'CCP_OK\nx\n')

cid = admin.post('/api/clusters', headers=ah,
                 json={'name': 'ai-train', 'kind': 'slurm'}).get_json()['id']
gid = admin.post('/api/clusters', headers=ah,
                 json={'name': 'plain', 'kind': 'generic'}).get_json()['id']
r = admin.post(f'/api/clusters/{gid}/slurm/generate', headers=ah, json={})
check('generic cluster refused', r.status_code == 400)
r = admin.post(f'/api/clusters/{cid}/slurm/generate', headers=ah, json={})
check('no members → 400', r.status_code == 400)

def mk(name, addr, state='managed'):
    nid = db.execute(
        "INSERT INTO nodes (name,address,conn,ssh_user,ssh_port,groups,created_at,state,cluster_id) "
        "VALUES (?,?,'ssh','root',22,'',?,?,?)",
        (name, addr, int(time.time()), state, cid))
    return nid

n1 = mk('rack1_sled1_gpu', '10.0.1.1')
n2 = mk('rack1_sled2_gpu', '10.0.1.2')
n3 = mk('pending-node', '10.0.1.3', state='discovered')
db.execute('INSERT INTO hardware (node_id, cpu_cores, mem_mb, gpu_count, updated_at) '
           'VALUES (?,?,?,?,?)', (n1, 64, 256000, 2, int(time.time())))

r = admin.post(f'/api/clusters/{cid}/slurm/generate', headers=ah,
               json={'controller_node_id': n3})
check('non-managed controller refused', r.status_code == 400)
r = admin.post(f'/api/clusters/{cid}/slurm/generate', headers=ah,
               json={'controller_node_id': n1})
d = r.get_json()
check('generate stores configs + warnings for unscanned member',
      r.status_code == 200 and 'ClusterName=ai-train' in d['slurm_conf']
      and any('rack1_sled2_gpu' in w for w in d['warnings']), d)
c = db.query('SELECT * FROM clusters WHERE id=?', (cid,), one=True)
check('configs persisted with controller',
      c['slurm_conf'] and c['controller_node_id'] == n1)
check('discovered member excluded from configs', 'pending-node' not in c['slurm_conf'])

print('== deploy ==')
r = admin.post(f'/api/clusters/{gid}/slurm/deploy', headers=ah)
check('deploy on generic cluster refused', r.status_code == 400)
r = admin.post(f'/api/clusters/{cid}/slurm/deploy', headers=ah)
check('deploy accepted', r.status_code == 202, r.get_json())
jid = r.get_json()['job_id']
job = db.query('SELECT * FROM jobs WHERE id=?', (jid,), one=True)
spec = json.loads(job['spec'])
check('deploy job is a playbook run against the managed members',
      job['kind'] == 'slurm_deploy' and sorted(spec['node_ids']) == [n1, n2],
      spec)
check('playbook embedded in spec carries the generated conf',
      'ClusterName=ai-train' in spec['playbook'])
# ansible-playbook isn't installed in the test env: the job fails at exec —
# lifecycle must NOT advance on a failed deploy
c = db.query('SELECT * FROM clusters WHERE id=?', (cid,), one=True)
check('failed deploy does not advance lifecycle', c['slurm_state'] == 'INIT',
      c['slurm_state'])

print('== lifecycle advance on success ==')
jid2 = executor.start_job('shell', 'x', {'node_ids': [], 'command': 'true',
                                         'cluster_id': cid, 'advance_to': 'DEPLOY'},
                          'admin')
# shell with no nodes exits 2 → no advance
check('failed job never advances', db.query(
    'SELECT slurm_state FROM clusters WHERE id=?', (cid,), one=True)['slurm_state'] == 'INIT')
lid = db.execute("INSERT INTO nodes (name,address,conn,ssh_user,ssh_port,groups,created_at,state) "
                 "VALUES ('local-x','localhost','local','root',22,'',?, 'managed')",
                 (int(time.time()),))
executor.start_job('shell', 'x', {'node_ids': [lid], 'command': 'true',
                                  'cluster_id': cid, 'advance_to': 'DEPLOY'}, 'admin')
check('successful job advances lifecycle', db.query(
    'SELECT slurm_state FROM clusters WHERE id=?', (cid,), one=True)['slurm_state'] == 'DEPLOY')

print('== RBAC ==')
admin.post('/api/users', headers=ah,
           json={'username': 'v3', 'password': 'password123', 'role': 'viewer'})
viewer, vh = client_for('v3', 'password123')
r = viewer.post(f'/api/clusters/{cid}/slurm/generate', headers=vh, json={})
check('viewer cannot generate', r.status_code == 403)
r = viewer.post(f'/api/clusters/{cid}/slurm/deploy', headers=vh)
check('viewer cannot deploy', r.status_code == 403)

print(f'\n{ok} passed, {fail} failed')
sys.exit(1 if fail else 0)
