"""Hardware discovery tests: fact-output parsing (GPU node, CPU-only node,
degraded output), persistence/rescan semantics, the hwscan API guard, and the
auto-chained scan after onboarding.
"""
import os, sys, time, tempfile, pathlib

TMP = tempfile.mkdtemp(prefix='ccp_hw_')
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

GPU_NODE_FACTS = """\
Warning: Permanently added '192.168.100.21' (ED25519) to the list of known hosts.
CCP_FACTS_BEGIN
os_name=Ubuntu 24.04.2 LTS
kernel=6.8.0-45-generic
arch=x86_64
cpu_model=AMD EPYC 7543 32-Core Processor
cpu_cores=128
cpu_sockets=2
threads_per_core=2
mem_kb=528280912
disk=nvme0n1|1920383410176
disk=sda|480103981056
nic=eno1|192.168.100.21
nic=ib0|10.10.0.21
gpu=NVIDIA H100 80GB HBM3
gpu=NVIDIA H100 80GB HBM3
gpu=NVIDIA H100 80GB HBM3
gpu=NVIDIA H100 80GB HBM3
ib=mlx5_0
CCP_FACTS_END
"""

CPU_NODE_FACTS = """\
CCP_FACTS_BEGIN
os_name=Debian GNU/Linux 12 (bookworm)
kernel=6.1.0-25-amd64
cpu_model=Intel(R) N100
cpu_cores=4
mem_kb=16305556
disk=sda|256060514304
nic=enp2s0|192.168.100.30
pci_gpu=00:02.0 VGA compatible controller: Intel Corporation Alder Lake-N [UHD Graphics]
CCP_FACTS_END
"""

print('== fact parsing: GPU node ==')
f = executor.parse_facts(GPU_NODE_FACTS)
check('os/kernel', f['os_name'] == 'Ubuntu 24.04.2 LTS' and f['kernel'] == '6.8.0-45-generic')
check('cpu summary', f['cpu_model'].startswith('AMD EPYC') and f['cpu_cores'] == 128
      and f['cpu_sockets'] == 2 and f['threads_per_core'] == 2)
check('memory in MB', f['mem_mb'] == 528280912 // 1024, f['mem_mb'])
check('disks humanized', 'nvme0n1 1.9TB' in f['disks'] and 'sda 480.1GB' in f['disks'], f['disks'])
check('nics', f['nics'] == 'eno1 192.168.100.21, ib0 10.10.0.21', f['nics'])
check('4 GPUs detected', f['gpu_count'] == 4 and f['gpu_model'] == 'NVIDIA H100 80GB HBM3')
check('infiniband', f['infiniband'] == 'mlx5_0')
check('ssh noise before markers ignored', 'known hosts' not in str(f['raw']))

print('== fact parsing: CPU-only node ==')
f = executor.parse_facts(CPU_NODE_FACTS)
check('integrated Intel VGA not counted as GPU', f['gpu_count'] == 0, f)
check('missing sockets tolerated', f['cpu_sockets'] is None and f['cpu_cores'] == 4)

print('== fact parsing: degraded output ==')
f = executor.parse_facts('CCP_FACTS_BEGIN\nkernel=5.4.0\nCCP_FACTS_END\n')
check('sparse facts do not crash', f['kernel'] == '5.4.0' and f['mem_mb'] is None
      and f['gpu_count'] == 0 and f['disks'] == '')

print('== hwscan job + persistence ==')
admin, ah = client_for('admin', 'adminpass123')
executor._password_ssh = lambda *a, **kw: (0, '')
executor._key_ssh = lambda a, u, p, cmd: (0, 'CCP_OK\ngpu-01\n') if 'CCP_OK' in cmd \
    else (0, GPU_NODE_FACTS)

r = admin.post('/api/nodes', headers=ah,
               json={'address': '192.168.100.21', 'username': 'ubuntu', 'password': 'pw'})
nid = r.get_json()['id']
n = db.query('SELECT * FROM nodes WHERE id=?', (nid,), one=True)
check('onboarded to managed', n['state'] == 'managed')
hw = db.query('SELECT * FROM hardware WHERE node_id=?', (nid,), one=True)
check('hardware auto-scanned after onboarding',
      hw is not None and hw['gpu_count'] == 4 and hw['cpu_cores'] == 128,
      dict(hw) if hw else None)
check('raw_json stored', 'H100' in hw['raw_json'])

print('== rescan replaces the row ==')
executor._key_ssh = lambda a, u, p, cmd: (0, CPU_NODE_FACTS)
r = admin.post(f'/api/nodes/{nid}/hwscan', headers=ah)
check('rescan accepted', r.status_code == 202, r.get_json())
hw = db.query('SELECT * FROM hardware WHERE node_id=?', (nid,), one=True)
rows = db.query('SELECT COUNT(*) AS c FROM hardware WHERE node_id=?', (nid,))[0]['c']
check('single row per node, values replaced',
      rows == 1 and hw['gpu_count'] == 0 and hw['cpu_cores'] == 4, dict(hw))

print('== guards ==')
did = db.execute("INSERT INTO nodes (name,address,conn,ssh_user,ssh_port,groups,"
                 "created_at,state) VALUES ('disc','10.0.0.9','ssh','root',22,'',?,"
                 "'discovered')", (int(time.time()),))
r = admin.post(f'/api/nodes/{did}/hwscan', headers=ah)
check('cannot scan a non-managed node', r.status_code == 400)

print('== failed scan keeps old facts ==')
executor._key_ssh = lambda a, u, p, cmd: (255, 'ssh: connect refused')
r = admin.post(f'/api/nodes/{nid}/hwscan', headers=ah)
job = db.query('SELECT * FROM jobs WHERE id=?', (r.get_json()['job_id'],), one=True)
hw = db.query('SELECT * FROM hardware WHERE node_id=?', (nid,), one=True)
check('scan job failed but old facts survive',
      job['status'] == 'failed' and hw['cpu_cores'] == 4)

print('== node delete cascades hardware ==')
admin.delete(f'/api/nodes/{nid}', headers=ah)
check('hardware row removed with node',
      db.query('SELECT * FROM hardware WHERE node_id=?', (nid,), one=True) is None)

print(f'\n{ok} passed, {fail} failed')
sys.exit(1 if fail else 0)
