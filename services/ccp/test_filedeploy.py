"""File deployment tests: staging listing, two-level target expansion, the
managed-only gate, source containment (a deploy must never read outside the
caller's own file space), both execution strategies, and the grouped output
markers the UI renders as nested collapsibles.
"""
import os, sys, io, json, time, tempfile, pathlib

TMP = tempfile.mkdtemp(prefix='ccp_deploy_')
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
pathlib.Path(f'{TMP}/ssh/id_ccp').write_text('k')
pathlib.Path(f'{TMP}/ssh/id_ccp.pub').write_text('ssh-ed25519 AAAA ccp')

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

def client_for(u, p):
    c = ccp.app.test_client()
    r = c.post('/login', data={'username': u, 'password': p})
    assert r.status_code in (302, 303)
    with c.session_transaction() as s:
        csrf = s['csrf']
    return c, {'X-CSRF-Token': csrf}

admin, ah = client_for('admin', 'adminpass123')

def mk(name, group, state='managed', conn='ssh'):
    return db.execute(
        "INSERT INTO nodes (name,address,conn,ssh_user,ssh_port,groups,created_at,state) "
        "VALUES (?,?,?,'root',22,?,?,?)",
        (name, 'localhost' if conn == 'local' else f'10.7.0.{mk.i}', conn, group,
         int(time.time()), state)) or None

mk.i = 0
def node(name, group, state='managed', conn='ssh'):
    mk.i += 1
    return db.execute(
        "INSERT INTO nodes (name,address,conn,ssh_user,ssh_port,groups,created_at,state) "
        "VALUES (?,?,?,'root',22,?,?,?)",
        (name, 'localhost' if conn == 'local' else f'10.7.0.{mk.i}', conn, group,
         int(time.time()), state))

n_web1 = node('web-01', 'web,prod')
n_web2 = node('web-02', 'web,prod')
n_db1 = node('db-01', 'db,prod')
n_pend = node('pending-01', 'web', state='discovered')
n_local = node('control', 'infra', conn='local')

print('== staging assets ==')
admin.post('/api/files', headers=ah, data={
    'folder': '', 'file': (io.BytesIO(b'x' * 2048), 'app.tar.gz')},
    content_type='multipart/form-data')
admin.post('/api/files', headers=ah, data={
    'folder': 'conf', 'file': (io.BytesIO(b'key=value\n'), 'site.conf')},
    content_type='multipart/form-data')
body = admin.get('/deploy').get_data(as_text=True)
check('deploy page lists staged files with sizes',
      'app.tar.gz' in body and 'conf/site.conf' in body and '2.0 kB' in body, body[:200])

print('== two-level target selector ==')
d = admin.get('/api/deploy/targets').get_json()
gnames = [g['name'] for g in d['groups']]
check('groups exposed from node tags', {'web', 'db', 'prod', 'infra'} <= set(gnames), gnames)
web = next(g for g in d['groups'] if g['name'] == 'web')
check('group exposes its individual nodes',
      {n['name'] for n in web['nodes']} == {'web-01', 'web-02', 'pending-01'},
      web['nodes'])
check('non-managed node marked ineligible',
      next(n for n in web['nodes'] if n['name'] == 'pending-01')['eligible'] is False)
check('managed node marked eligible',
      next(n for n in web['nodes'] if n['name'] == 'web-01')['eligible'] is True)

print('== validation ==')
r = admin.post('/api/deploy/files', headers=ah,
               json={'files': [], 'node_ids': [n_web1], 'dest': '/opt/a'})
check('no files → 400', r.status_code == 400)
r = admin.post('/api/deploy/files', headers=ah,
               json={'files': ['app.tar.gz'], 'node_ids': [n_web1], 'dest': 'relative/path'})
check('relative destination rejected', r.status_code == 400, r.get_json())
r = admin.post('/api/deploy/files', headers=ah,
               json={'files': ['app.tar.gz'], 'node_ids': [n_web1],
                     'dest': '/opt/a; rm -rf /'})
check('destination with shell metacharacters rejected', r.status_code == 400)
r = admin.post('/api/deploy/files', headers=ah,
               json={'files': ['app.tar.gz'], 'node_ids': [], 'groups': [],
                     'dest': '/opt/a'})
check('no targets → 400', r.status_code == 400)
r = admin.post('/api/deploy/files', headers=ah,
               json={'files': ['app.tar.gz'], 'node_ids': [n_pend], 'dest': '/opt/a'})
check('non-managed target → 400 naming it',
      r.status_code == 400 and 'pending-01' in r.get_json()['error'], r.get_json())

print('== source containment (never outside the caller\'s space) ==')
pathlib.Path(f'{TMP}/secret.txt').write_text('other-user data')
for bad in ('../secret.txt', '/etc/passwd', '../../etc/passwd', 'nope.tar'):
    r = admin.post('/api/deploy/files', headers=ah,
                   json={'files': [bad], 'node_ids': [n_web1], 'dest': '/opt/a'})
    check(f'source {bad!r} refused', r.status_code in (400, 403, 404), r.status_code)

print('== group expansion + managed-only gate ==')
calls = []
def fake_run(cmd, **kw):
    calls.append(cmd)
    class P:
        returncode = 0
        stdout = 'copied\n'
        stderr = ''
    return P()
executor.subprocess.run = fake_run

r = admin.post('/api/deploy/files', headers=ah,
               json={'files': ['app.tar.gz', 'conf/site.conf'],
                     'groups': ['web'], 'dest': '/opt/assets', 'method': 'clush'})
d = r.get_json()
check('group deploy accepted', r.status_code == 202, d)
check('group expanded to its managed nodes only',
      sorted(d['nodes']) == ['web-01', 'web-02'], d['nodes'])
check('ineligible group member reported as excluded',
      any('pending-01' in e for e in d['excluded']), d['excluded'])

log = executor.job_log(d['job_id'])
check('log emits a group marker', '##GROUP## web' in log, log[:400])
check('log emits per-host headers with STATUS',
      '| STATUS: CHANGED =====' in log and 'web-01' in log and 'web-02' in log, log[:600])
check('both files named in the log with sizes',
      'app.tar.gz' in log and 'site.conf' in log and '2' in log)
clush = [c for c in calls if c and c[0] == 'clush']
check('clush invoked with -w nodelist, --copy and --dest',
      clush and '--copy' in clush[0] and '--dest' in clush[0]
      and clush[0][clush[0].index('-w') + 1].count(',') == 1, clush[:1])
check('clush --dest carries the requested destination',
      clush and clush[0][clush[0].index('--dest') + 1] == '/opt/assets')

print('== mixed targeting: a group plus an individual node ==')
r = admin.post('/api/deploy/files', headers=ah,
               json={'files': ['app.tar.gz'], 'groups': ['web'],
                     'node_ids': [n_db1], 'dest': '/opt/assets'})
d = r.get_json()
check('mixed group + node selection resolves to the union',
      sorted(d['nodes']) == ['db-01', 'web-01', 'web-02'], d['nodes'])
log = executor.job_log(d['job_id'])
check('output grouped per infrastructure group',
      '##GROUP## web' in log and '##GROUP## db' in log, log[:500])

print('== ansible strategy ==')
RECAP = ('PLAY RECAP *****\n'
         'web-01 : ok=2 changed=1 unreachable=0 failed=0 skipped=0\n'
         'web-02 : ok=2 changed=0 unreachable=0 failed=0 skipped=0\n')
def fake_ansible(cmd, **kw):
    calls.append(cmd)
    class P:
        returncode = 0
        stdout = RECAP
        stderr = ''
    return P()
executor.subprocess.run = fake_ansible
r = admin.post('/api/deploy/files', headers=ah,
               json={'files': ['app.tar.gz'], 'groups': ['web'],
                     'dest': '/opt/assets', 'method': 'ansible'})
d = r.get_json()
log = executor.job_log(d['job_id'])
check('ansible strategy used', any(c and c[0] == 'ansible-playbook' for c in calls))
check('changed host reported CHANGED',
      'web-01' in log and 'STATUS: CHANGED' in log, log[:600])
check('unchanged host reported SUCCESS', 'STATUS: SUCCESS' in log, log[:600])

print('== local node uses a direct copy ==')
executor.subprocess.run = fake_run
r = admin.post('/api/deploy/files', headers=ah,
               json={'files': ['app.tar.gz'], 'node_ids': [n_local],
                     'dest': f'{TMP}/localdest'})
log = executor.job_log(r.get_json()['job_id'])
check('local target copied without ssh',
      'control' in log and 'local) | STATUS: CHANGED' in log, log[:400])

print('== RBAC ==')
admin.post('/api/users', headers=ah,
           json={'username': 'v9', 'password': 'password123', 'role': 'viewer'})
viewer, vh = client_for('v9', 'password123')
r = viewer.post('/api/deploy/files', headers=vh,
                json={'files': ['app.tar.gz'], 'node_ids': [n_web1], 'dest': '/opt/a'})
check('viewer cannot deploy files', r.status_code == 403)
check('viewer can view targets', viewer.get('/api/deploy/targets').status_code == 200)

print(f'\n{ok} passed, {fail} failed')
sys.exit(1 if fail else 0)
