"""Topology tests: hostname parsing, migration backfill, the hostname-change
job (script success/failure paths via a stubbed SSH), and immediate inventory
refresh including rack/sled/role.
"""
import os, sys, time, sqlite3, tempfile, pathlib

TMP = tempfile.mkdtemp(prefix='ccp_topo_')
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
import topology                                              # noqa: E402

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

def node(nid):
    return db.query('SELECT * FROM nodes WHERE id=?', (nid,), one=True)

print('== hostname parsing ==')
cases = [
    ('rack0_sled1_gpu', 0, 1, 'gpu'),
    ('rack12-sled3-cpu', 12, 3, 'cpu'),
    ('RACK2_SLED10_GPU', 2, 10, 'gpu'),
    ('rack1_sled4', 1, 4, ''),
    ('rack0_sled1_a100', 0, 1, 'a100'),
]
for name, rack, sled, role in cases:
    t = topology.parse(name)
    check(f'parse {name}', t == {'rack': rack, 'sled': sled, 'role': role}, t)
for name in ('web-01', 'racks_sled1_gpu', 'rack0sled1', 'rack0_sled1_gpu_x_y-',
             'rack_sled_gpu', '', 'sled1_rack0_gpu'):
    t = topology.parse(name)
    check(f'non-matching {name!r} → no topology', t['rack'] is None and t['role'] == '', t)

print('== onboarding applies topology from adopted hostname ==')
admin, ah = client_for('admin', 'adminpass123')
executor._password_ssh = lambda *a, **kw: (0, '')
executor._key_ssh = lambda a, u, p, cmd: (0, 'CCP_OK\nrack3_sled7_gpu\n')
r = admin.post('/api/nodes', headers=ah,
               json={'address': '192.168.100.40', 'username': 'u', 'password': 'p'})
n = node(r.get_json()['id'])
check('adopted name parsed into rack/sled/role',
      n['rack'] == 3 and n['sled'] == 7 and n['role'] == 'gpu', dict(n))

print('== hostname change API guards ==')
nid = n['id']
r = admin.post(f'/api/nodes/{nid}/hostname', headers=ah, json={'hostname': 'bad name!'})
check('invalid hostname rejected', r.status_code == 400)
r = admin.post(f'/api/nodes/{nid}/hostname', headers=ah, json={'hostname': 'rack3_sled7_gpu'})
check('same name rejected', r.status_code == 400)
did = db.execute("INSERT INTO nodes (name,address,conn,ssh_user,ssh_port,groups,created_at,state) "
                 "VALUES ('pending','10.0.0.7','ssh','root',22,'',?, 'discovered')",
                 (int(time.time()),))
r = admin.post(f'/api/nodes/{did}/hostname', headers=ah, json={'hostname': 'x1'})
check('non-managed node cannot be renamed', r.status_code == 400)
r = admin.post(f'/api/nodes/{nid}/hostname', headers=ah, json={'hostname': 'pending'})
check('name collision → 409', r.status_code == 409)

print('== hostname change job: success refreshes inventory immediately ==')
captured = {}
def _rename_ok(a, u, p, cmd):
    captured['cmd'] = cmd
    return (0, 'CCP_HOSTNAME_ACTUAL rack4_sled1_cpu\n'
               'CCP_HOSTNAME_OK rack4_sled1_cpu\n')
executor._key_ssh = _rename_ok
r = admin.post(f'/api/nodes/{nid}/hostname', headers=ah,
               json={'hostname': 'rack4_sled1_cpu'})
check('rename accepted', r.status_code == 202, r.get_json())
n = node(nid)
check('inventory name updated immediately', n['name'] == 'rack4_sled1_cpu', dict(n))
check('topology recomputed', n['rack'] == 4 and n['sled'] == 1 and n['role'] == 'cpu')
check('remote script uses hostnamectl with /etc/hostname fallback',
      'hostnamectl set-hostname' in captured['cmd'] and '/etc/hostname' in captured['cmd']
      and '/etc/hosts' in captured['cmd'], captured['cmd'])
check('script handles non-root via sudo -n', 'sudo -n' in captured['cmd'])
check('script re-reads the live hostname and fails if it did not change',
      'CCP_HOSTNAME_ACTUAL' in captured['cmd'] and 'exit 43' in captured['cmd'],
      captured['cmd'])
check('script survives hostnamectl refusing the name (falls back, no abort)',
      'hostnamectl refused' in captured['cmd'] and 'set_ok' in captured['cmd'])
check('script stops cloud-init reverting the hostname on reboot',
      'preserve_hostname: true' in captured['cmd'])

print('== rename that does not take must NOT update the inventory ==')
# the box accepts the command but keeps its old hostname (systemd refusing an
# underscore, hostnamed unavailable, cloud-init, static-vs-transient). CCP must
# never record a name the machine does not answer to — that mismatch is what
# breaks Slurm's identity checks.
before = node(nid)['name']
executor._key_ssh = lambda a, u, p, cmd: (
    43, 'CCP_HOSTNAME_ACTUAL gpu-node\nCCP_ERR: hostname is still \'gpu-node\' '
        "after the change\n")
r = admin.post(f'/api/nodes/{nid}/hostname', headers=ah,
               json={'hostname': 'rack7_sled7_gpu'})
jid = r.get_json()['job_id']
check('silent no-op rename fails the job', db.query(
    'SELECT status FROM jobs WHERE id=?', (jid,), one=True)['status'] == 'failed')
check('inventory name NOT updated on a no-op rename',
      node(nid)['name'] == before, node(nid)['name'])
log = executor.job_log(jid)
check('log reports what the node actually reports', 'gpu-node' in log, log)
check('underscore name gets the hyphen hint',
      'rack7-sled7-gpu' in log, log)
check('log states the inventory was left alone',
      'inventory left unchanged' in log, log)

# a box that reports a DIFFERENT name than requested (not the old one either)
executor._key_ssh = lambda a, u, p, cmd: (
    0, 'CCP_HOSTNAME_ACTUAL something-else\nCCP_HOSTNAME_OK something-else\n')
before = node(nid)['name']
r = admin.post(f'/api/nodes/{nid}/hostname', headers=ah, json={'hostname': 'rack8-sled1-cpu'})
check('rename reporting a different name is rejected',
      node(nid)['name'] == before, node(nid)['name'])

print('== hostname change job: failure leaves inventory untouched ==')
executor._key_ssh = lambda a, u, p, cmd: (40, 'CCP_ERR: not root and passwordless sudo unavailable\n')
r = admin.post(f'/api/nodes/{nid}/hostname', headers=ah, json={'hostname': 'rack9_sled9_gpu'})
jid = r.get_json()['job_id']
n = node(nid)
check('failed rename keeps old name', n['name'] == 'rack4_sled1_cpu', dict(n))
check('node stays managed after failed rename', n['state'] == 'managed')
job = db.query('SELECT * FROM jobs WHERE id=?', (jid,), one=True)
check('job failed with sudo hint', job['status'] == 'failed'
      and 'sudo' in executor.job_log(jid))

print('== rack view page ==')
r = admin.get('/rack')
body = r.get_data(as_text=True)
check('rack view renders racks from topology',
      'Rack 4' in body and 'rack4_sled1_cpu' in body, r.status_code)
check('non-topology nodes listed as unracked', 'pending' in body)

print('== M3 backfill on legacy DB ==')
legacy = f'{TMP}/legacy.db'
conn = sqlite3.connect(legacy)
conn.executescript("""
CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'viewer', created_at INTEGER NOT NULL);
CREATE TABLE nodes (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
  address TEXT NOT NULL, conn TEXT NOT NULL DEFAULT 'ssh', ssh_user TEXT NOT NULL DEFAULT 'root',
  ssh_port INTEGER NOT NULL DEFAULT 22, groups TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL);
INSERT INTO users (username, password_hash, role, created_at) VALUES ('a','x','admin',0);
INSERT INTO nodes (name, address, created_at) VALUES ('rack1_sled2_gpu','10.1.1.1',0);
INSERT INTO nodes (name, address, created_at) VALUES ('plain-node','10.1.1.2',0);
""")
conn.commit(); conn.close()
_orig = db.DB_PATH
db.DB_PATH = legacy
db.init_db()
db.DB_PATH = _orig
conn = sqlite3.connect(legacy)
conn.row_factory = sqlite3.Row
rows = {r['name']: r for r in conn.execute('SELECT * FROM nodes')}
check('backfill parsed rack names',
      rows['rack1_sled2_gpu']['rack'] == 1 and rows['rack1_sled2_gpu']['sled'] == 2
      and rows['rack1_sled2_gpu']['role'] == 'gpu')
check('backfill left plain names unracked', rows['plain-node']['rack'] is None)
conn.close()


print('== /etc/hosts is rewritten in place, never appended ==')
import subprocess, tempfile as _tf
script = executor._hostname_script('rack0-sled2-gpu')
stage = ('new=rack0-sled2-gpu\nSUDO=\n# Rewrite the canonical'
         + script.split('# Rewrite the canonical')[1].split('# cloud images')[0])
BROKEN = """127.0.0.1 localhost
127.0.1.1 gpu-node

10.10.90.74   rack0-sled2-gpu
10.10.90.104  rack0-sled1-cpu

# The following lines are desirable for IPv6 capable hosts
::1     ip6-localhost ip6-loopback
ff02::2 ip6-allrouters
127.0.1.1\track0_sled2_gpu
127.0.1.1\track0-sled2-gpu
"""
d = _tf.mkdtemp()
hosts = pathlib.Path(d) / 'hosts'
hosts.write_text(BROKEN)
sh = pathlib.Path(d) / 's.sh'
sh.write_text(stage.replace('/etc/hosts', str(hosts)))
rc = subprocess.run(['sh', str(sh)], capture_output=True, text=True).returncode
out = hosts.read_text()
check('hosts stage succeeds', rc == 0)
check('exactly one 127.0.1.1 entry remains',
      len([l for l in out.splitlines() if l.startswith('127.0.1.1')]) == 1, out)
check('the 127.0.1.1 entry carries the new hostname',
      any(l.startswith('127.0.1.1') and l.endswith('rack0-sled2-gpu')
          for l in out.splitlines()), out)
check('the stale hostname is gone', 'gpu-node' not in out, out)
check('duplicate appended underscore entry cleaned up',
      'rack0_sled2_gpu' not in out, out)
check('peer entries preserved',
      '10.10.90.74   rack0-sled2-gpu' in out and '10.10.90.104  rack0-sled1-cpu' in out, out)
check('ipv6 block preserved', 'ip6-allrouters' in out and 'ip6-localhost' in out)
check('localhost line preserved', out.splitlines()[0] == '127.0.0.1 localhost')

# idempotent: running it again changes nothing
before = out
subprocess.run(['sh', str(sh)], capture_output=True, text=True)
check('re-running the rename is idempotent', hosts.read_text() == before)

print('== ssh noise suppressed at the source ==')
check('key ssh passes LogLevel=ERROR', 'LogLevel=ERROR' in ' '.join(executor.SSH_COMMON))

print(f'\n{ok} passed, {fail} failed')
sys.exit(1 if fail else 0)
