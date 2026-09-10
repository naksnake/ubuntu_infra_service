"""Cluster tests: CRUD, membership move semantics, delete clearing
membership, and cluster_id as a run target expanding to managed members only.
"""
import os, sys, time, tempfile, pathlib

TMP = tempfile.mkdtemp(prefix='ccp_clusters_')
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

_ip = [1]
def mk(name, state='managed', conn='ssh'):
    _ip[0] += 1
    return db.execute(
        "INSERT INTO nodes (name,address,conn,ssh_user,ssh_port,groups,created_at,state) "
        "VALUES (?,?,?,'root',22,'',?,?)",
        (name, 'localhost' if conn == 'local' else f'10.9.0.{_ip[0]}',
         conn, int(time.time()), state))

admin, ah = client_for('admin', 'adminpass123')

print('== CRUD ==')
r = admin.post('/api/clusters', headers=ah, json={'name': 'bad name!'})
check('invalid name rejected', r.status_code == 400)
r = admin.post('/api/clusters', headers=ah,
               json={'name': 'ai-train', 'kind': 'slurm', 'description': 'H100s'})
check('create cluster', r.status_code == 201, r.get_json())
cid = r.get_json()['id']
r = admin.post('/api/clusters', headers=ah, json={'name': 'ai-train'})
check('duplicate name → 409', r.status_code == 409)
r = admin.get('/api/clusters')
c = r.get_json()['clusters'][0]
check('every cluster is a plain execution target (a requested kind is ignored)',
      c['kind'] == 'generic' and c['description'] == 'H100s', c)

print('== membership ==')
n1, n2, n3 = mk('rack0_sled1_gpu'), mk('rack0_sled2_gpu'), mk('pending', state='discovered')
nl = mk('control', conn='local')
r = admin.post(f'/api/clusters/{cid}/nodes', headers=ah, json={'node_ids': [n1, n2, n3, nl]})
check('assign nodes', r.status_code == 200, r.get_json())
r = admin.get('/api/clusters')
c = r.get_json()['clusters'][0]
check('member/managed counts', c['members'] == 4 and c['managed'] == 3, c)

r = admin.post('/api/clusters', headers=ah, json={'name': 'second'})
cid2 = r.get_json()['id']
admin.post(f'/api/clusters/{cid2}/nodes', headers=ah, json={'node_ids': [n2]})
row = db.query('SELECT cluster_id FROM nodes WHERE id=?', (n2,), one=True)
check('assign moves node between clusters', row['cluster_id'] == cid2)

r = admin.delete(f'/api/clusters/{cid}/nodes/{n3}', headers=ah)
check('unassign node', r.status_code == 200
      and db.query('SELECT cluster_id FROM nodes WHERE id=?', (n3,), one=True)['cluster_id'] is None)

print('== cluster as run target ==')
r = admin.post('/api/run/shell', headers=ah,
               json={'command': 'echo cluster-run', 'cluster_id': cid})
check('run on cluster accepted', r.status_code == 201, r.get_json())
out = executor.job_log(r.get_json()['job_id'])
check('local member executed', 'cluster-run' in out, out)
job = db.query('SELECT * FROM jobs ORDER BY id DESC LIMIT 1', one=True)
check('target names only eligible members',
      'control' in job['target'] and 'rack0_sled1_gpu' in job['target']
      and 'pending' not in job['target'] and 'rack0_sled2_gpu' not in job['target'],
      job['target'])

r = admin.post('/api/run/shell', headers=ah,
               json={'command': 'echo x', 'cluster_id': cid2})
# cid2's only member (n2) is managed → runs; ClusterShell missing locally makes
# the job fail at execution, but the API must accept the target expansion
check('cluster with only ssh members accepted', r.status_code == 201)

empty = admin.post('/api/clusters', headers=ah, json={'name': 'empty'}).get_json()['id']
r = admin.post('/api/run/shell', headers=ah, json={'command': 'echo x', 'cluster_id': empty})
check('empty cluster → 400', r.status_code == 400)

print('== delete cluster keeps nodes ==')
r = admin.delete(f'/api/clusters/{cid}', headers=ah)
check('delete cluster', r.status_code == 200)
check('membership cleared, nodes kept',
      db.query('SELECT COUNT(*) AS c FROM nodes WHERE cluster_id=?', (cid,))[0]['c'] == 0
      and db.query('SELECT COUNT(*) AS c FROM nodes')[0]['c'] == 4)

print('== RBAC ==')
admin.post('/api/users', headers=ah,
           json={'username': 'v2', 'password': 'password123', 'role': 'viewer'})
viewer, vh = client_for('v2', 'password123')
r = viewer.post('/api/clusters', headers=vh, json={'name': 'nope'})
check('viewer cannot create clusters', r.status_code == 403)
r = viewer.get('/api/clusters')
check('viewer can list clusters', r.status_code == 200)

print('== pages render ==')
r = admin.get('/clusters')
check('clusters page renders', r.status_code == 200 and b'second' in r.data)
r = admin.get('/shell?cluster=' + str(cid2))
check('shell page renders with cluster preselect', r.status_code == 200
      and b'cluster-input' in r.data)

print(f'\n{ok} passed, {fail} failed')
sys.exit(1 if fail else 0)
