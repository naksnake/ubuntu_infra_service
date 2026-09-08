"""Node lifecycle tests: onboarding (credential validation → key bootstrap →
execution check), verify, failure states, secret hygiene, the managed-only
targeting gate, and the legacy-schema migration.

SSH never happens: executor._password_ssh/_key_ssh are stubbed. Jobs run
inline (CCP_TEST_SYNC_JOBS=1) so state transitions are deterministic.
"""
import os, sys, json, sqlite3, tempfile, pathlib, time

TMP = tempfile.mkdtemp(prefix='ccp_lifecycle_')
os.environ.update({
    'CCP_DB': f'{TMP}/ccp.db',
    'CCP_FILES_DIR': f'{TMP}/files',
    'CCP_JOBS_DIR': f'{TMP}/jobs',
    'CCP_SSH_KEY': f'{TMP}/ssh/id_ccp',
    'CCP_ADMIN_USER': 'admin',
    'CCP_ADMIN_PASSWORD': 'adminpass123',
    'CCP_TEST_SYNC_JOBS': '1',
})
# pre-create a fake keypair so ensure_ssh_key() is a no-op and public_key() works
pathlib.Path(f'{TMP}/ssh').mkdir(parents=True)
pathlib.Path(f'{TMP}/ssh/id_ccp').write_text('FAKE PRIVATE KEY\n')
pathlib.Path(f'{TMP}/ssh/id_ccp.pub').write_text('ssh-ed25519 AAAATESTKEY ccp-test\n')

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
    assert r.status_code in (302, 303), f'login failed for {username}: {r.status_code}'
    with c.session_transaction() as s:
        csrf = s['csrf']
    return c, {'X-CSRF-Token': csrf}

def node(node_id):
    return db.query('SELECT * FROM nodes WHERE id=?', (node_id,), one=True)

# stub registry: tests set these to control each SSH step
class Stub:
    password = (0, '')                 # rc/output for _password_ssh
    key = (0, 'CCP_OK\nrack0_sled1_gpu\n')
    password_calls = []
    key_calls = []

def _fake_password_ssh(address, user, port, command, password):
    Stub.password_calls.append((address, user, port, command, password))
    return Stub.password

def _fake_key_ssh(address, user, port, command):
    Stub.key_calls.append((address, user, port, command))
    return Stub.key

executor._password_ssh = _fake_password_ssh
executor._key_ssh = _fake_key_ssh

admin, ah = client_for('admin', 'adminpass123')

print('== validation ==')
r = admin.post('/api/nodes', headers=ah, json={'address': '10.0.0.5'})
check('ssh onboard without credentials → 400', r.status_code == 400, r.get_json())
r = admin.post('/api/nodes', headers=ah,
               json={'address': 'bad host!', 'username': 'u', 'password': 'p'})
check('invalid address rejected', r.status_code == 400)
r = admin.post('/api/nodes', headers=ah,
               json={'address': '10.0.0.5', 'username': 'u;rm', 'password': 'p'})
check('invalid ssh user rejected', r.status_code == 400)

print('== happy path: onboard walks to managed, adopts remote hostname ==')
Stub.password, Stub.key = (0, ''), (0, 'CCP_OK\nrack0_sled1_gpu\n')
SECRET = 'S3cret-Pa55w0rd!'
r = admin.post('/api/nodes', headers=ah,
               json={'address': '192.168.100.21', 'username': 'ubuntu',
                     'password': SECRET})
d = r.get_json()
check('onboard accepted (201 with node id + job id)',
      r.status_code == 201 and d.get('id') and d.get('job_id'), d)
nid, jid = d['id'], d['job_id']
n = node(nid)
check('node state is managed', n['state'] == 'managed', dict(n))
check('onboarded_at set', bool(n['onboarded_at']))
check('name adopted from remote hostname', n['name'] == 'rack0_sled1_gpu', n['name'])
check('ssh user recorded', n['ssh_user'] == 'ubuntu')
log = executor.job_log(jid)
check('job log shows the three steps',
      '[1/3]' in log and '[2/3]' in log and '[3/3]' in log and 'MANAGED' in log, log)
check('key install command is idempotent (grep guard)',
      any('grep -qxF' in c[3] and 'authorized_keys' in c[3] for c in Stub.password_calls))

print('== secret hygiene: the password exists nowhere on disk ==')
job = db.query('SELECT * FROM jobs WHERE id=?', (jid,), one=True)
check('password not in jobs.spec', SECRET not in job['spec'], job['spec'])
check('password not in job log', SECRET not in log)
audit_all = ' '.join(f"{a['action']} {a['detail']}" for a in db.query('SELECT * FROM audit'))
check('password not in audit log', SECRET not in audit_all)
raw = b''
for suffix in ('', '-wal', '-shm'):
    p = pathlib.Path(f'{TMP}/ccp.db{suffix}')
    if p.exists():
        raw += p.read_bytes()
check('password not in raw database bytes', SECRET.encode() not in raw)
logs_raw = b''.join(p.read_bytes() for p in pathlib.Path(f'{TMP}/jobs').glob('*.log'))
check('password not in any job log file', SECRET.encode() not in logs_raw)

print('== failure paths ==')
Stub.password = (5, 'Permission denied')
r = admin.post('/api/nodes', headers=ah,
               json={'address': '192.168.100.22', 'username': 'root', 'password': 'wrong'})
nid2 = r.get_json()['id']
n = node(nid2)
check('wrong password → failed + auth reason',
      n['state'] == 'failed' and 'auth failed' in n['state_detail'], dict(n))

Stub.password = (-1, 'timed out after 60s')
r = admin.post('/api/nodes', headers=ah,
               json={'address': '192.168.100.23', 'username': 'root', 'password': 'x'})
n = node(r.get_json()['id'])
check('timeout → failed + unreachable reason',
      n['state'] == 'failed' and 'unreachable' in n['state_detail'], dict(n))

Stub.password, Stub.key = (0, ''), (255, 'Permission denied (publickey)')
r = admin.post('/api/nodes', headers=ah,
               json={'address': '192.168.100.24', 'username': 'root', 'password': 'x'})
n = node(r.get_json()['id'])
check('key-auth check fails → failed', n['state'] == 'failed'
      and 'execution check failed' in n['state_detail'], dict(n))

print('== retry a failed node ==')
Stub.password, Stub.key = (0, ''), (0, 'CCP_OK\nweb-02\n')
r = admin.post(f'/api/nodes/{nid2}/onboard', headers=ah,
               json={'username': 'ubuntu', 'password': 'now-right'})
check('re-onboard accepted', r.status_code == 202, r.get_json())
n = node(nid2)
check('retried node is managed', n['state'] == 'managed', dict(n))
check('ssh user updated on retry', n['ssh_user'] == 'ubuntu')

print('== onboarding-in-progress guard ==')
db.execute("UPDATE nodes SET state='onboarding' WHERE id=?", (nid2,))
r = admin.post(f'/api/nodes/{nid2}/onboard', headers=ah,
               json={'username': 'u', 'password': 'p'})
check('concurrent onboard → 409', r.status_code == 409)
db.execute("UPDATE nodes SET state='managed' WHERE id=?", (nid2,))

print('== verify (legacy unverified rows) ==')
lid = db.execute("INSERT INTO nodes (name, address, conn, ssh_user, ssh_port, groups, "
                 "created_at, state) VALUES ('legacy-01','192.168.100.30','ssh','root',22,'',?, 'unverified')",
                 (int(time.time()),))
Stub.key = (0, 'CCP_OK\nlegacy-01\n')
r = admin.post(f'/api/nodes/{lid}/verify', headers=ah)
check('verify accepted', r.status_code == 202, r.get_json())
check('unverified → managed after key check', node(lid)['state'] == 'managed')

lid2 = db.execute("INSERT INTO nodes (name, address, conn, ssh_user, ssh_port, groups, "
                  "created_at, state) VALUES ('legacy-02','192.168.100.31','ssh','root',22,'',?, 'unverified')",
                  (int(time.time()),))
Stub.key = (255, 'Permission denied (publickey)')
admin.post(f'/api/nodes/{lid2}/verify', headers=ah)
check('failed verify → failed state', node(lid2)['state'] == 'failed')
Stub.key = (0, 'CCP_OK\nx\n')

print('== targeting gate: only managed/local nodes run jobs ==')
r = admin.post('/api/run/shell', headers=ah,
               json={'command': 'echo hi', 'node_ids': [lid2]})
check('run against failed node → 400 naming it',
      r.status_code == 400 and 'legacy-02' in r.get_json()['error'], r.get_json())

locid = db.execute("INSERT INTO nodes (name, address, conn, ssh_user, ssh_port, groups, "
                   "created_at, state) VALUES ('control','localhost','local','root',22,'',?, 'managed')",
                   (int(time.time()),))
r = admin.post('/api/run/shell', headers=ah,
               json={'command': 'echo gate-ok', 'node_ids': [locid, lid2]})
check('mixed selection runs (only eligible nodes)', r.status_code == 201, r.get_json())
out = executor.job_log(r.get_json()['job_id'])
check('local node executed', 'gate-ok' in out, out)
check('failed node not executed', 'legacy-02' not in out.split('=====')[0] and
      '192.168.100.31' not in out, out)

print('== local node API path ==')
r = admin.post('/api/nodes', headers=ah,
               json={'conn': 'local', 'name': 'ctl2', 'address': 'localhost'})
check('local add needs no credentials', r.status_code == 201, r.get_json())
check('local node is managed immediately', node(r.get_json()['id'])['state'] == 'managed')

print('== RBAC ==')
admin.post('/api/users', headers=ah,
           json={'username': 'viewer1', 'password': 'password123', 'role': 'viewer'})
viewer, vh = client_for('viewer1', 'password123')
r = viewer.post('/api/nodes', headers=vh,
                json={'address': '10.0.0.9', 'username': 'u', 'password': 'p'})
check('viewer cannot onboard nodes', r.status_code == 403)
r = viewer.post(f'/api/nodes/{lid}/verify', headers=vh)
check('viewer cannot verify nodes', r.status_code == 403)
r = viewer.get('/api/nodes')
check('viewer can read inventory', r.status_code == 200)

print('== legacy schema migration (M1) ==')
legacy_db = f'{TMP}/legacy.db'
conn = sqlite3.connect(legacy_db)
conn.executescript("""
CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'viewer', created_at INTEGER NOT NULL);
CREATE TABLE nodes (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
  address TEXT NOT NULL, conn TEXT NOT NULL DEFAULT 'ssh', ssh_user TEXT NOT NULL DEFAULT 'root',
  ssh_port INTEGER NOT NULL DEFAULT 22, groups TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL);
INSERT INTO users (username, password_hash, role, created_at) VALUES ('a','x','admin',0);
INSERT INTO nodes (name, address, conn, created_at) VALUES ('old-ssh','10.1.1.1','ssh',0);
INSERT INTO nodes (name, address, conn, created_at) VALUES ('old-local','localhost','local',0);
""")
conn.commit(); conn.close()
_orig = db.DB_PATH
db.DB_PATH = legacy_db
db.init_db()
db.DB_PATH = _orig
conn = sqlite3.connect(legacy_db)
conn.row_factory = sqlite3.Row
rows = {r['name']: r for r in conn.execute('SELECT * FROM nodes')}
check('legacy ssh row → unverified', rows['old-ssh']['state'] == 'unverified')
check('legacy local row → managed', rows['old-local']['state'] == 'managed')
cols = [r['name'] for r in conn.execute('PRAGMA table_info(nodes)')]
check('M1 columns added in place',
      all(c in cols for c in ('state', 'state_detail', 'mac', 'onboarded_at')))
conn.close()

print(f'\n{ok} passed, {fail} failed')
sys.exit(1 if fail else 0)
