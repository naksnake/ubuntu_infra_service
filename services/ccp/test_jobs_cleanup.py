"""Job history clean-up: stats, the one clear-all action (every finished job
and log file, orphan logs too, running jobs kept), single delete that removes
the log file, automatic retention, the Jobs page and RBAC.
"""
import os, sys, time, tempfile, pathlib

TMP = tempfile.mkdtemp(prefix='ccp_jobsclean_')
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

DAY = 86400
NOW = int(time.time())

def mkjob(kind, status, age_days, size=1000):
    """A job row plus a log file of `size` bytes, finished `age_days` ago."""
    created = NOW - age_days * DAY - 60
    finished = None if status == 'running' else created + 30
    jid = db.execute('INSERT INTO jobs (kind,target,spec,status,exit_code,created_by,created_at,finished_at) '
                     'VALUES (?,?,?,?,?,?,?,?)',
                     (kind, 'x', '{}', status, None if status == 'running' else (0 if status == 'success' else 1),
                      'admin', created, finished))
    pathlib.Path(executor._log_path(jid)).write_bytes(b'x' * size)
    return jid

def exists(jid):
    return os.path.exists(executor._log_path(jid))

def rows():
    return {r['id'] for r in db.query('SELECT id FROM jobs')}

admin, ah = client_for('admin', 'adminpass123')

finished = [mkjob('shell', 'success', 30, 5000), mkjob('ansible', 'failed', 20, 8000),
            mkjob('hwscan', 'success', 1, 2000), mkjob('filedeploy', 'failed', 0, 3000)]
running = mkjob('shell', 'running', 1, 100)
stray = pathlib.Path(f'{TMP}/jobs/999999.log')
stray.write_bytes(b'o' * 4096)
pathlib.Path(f'{TMP}/jobs/notes.txt').write_text('not a job log')

print('== stats ==')
r = admin.get('/api/jobs/stats')
s = r.get_json()
check('counts per status', r.status_code == 200 and s['total'] == 5 and s['running'] == 1
      and s['success'] == 2 and s['failed'] == 2, s)
check('log files and bytes on disk (stray .log counted, notes.txt not)',
      s['log_files'] == 6 and s['log_bytes'] == 5000 + 8000 + 2000 + 3000 + 100 + 4096, s)
check('retention off by default', s['retention_days'] == 0)

print('== single delete ==')
r = admin.delete(f'/api/jobs/{running}', headers=ah)
check('a running job cannot be deleted (409)', r.status_code == 409 and running in rows(), r.get_json())
victim = finished.pop()
r = admin.delete(f'/api/jobs/{victim}', headers=ah)
check('single delete removes the row AND its log file',
      r.status_code == 200 and victim not in rows() and not exists(victim))

print('== clear all history ==')
r = admin.post('/api/jobs/cleanup', headers=ah, json={})
d = r.get_json()
check('every finished job and the orphan log are gone, bytes reported',
      r.status_code == 200 and d['deleted'] == 3 and d['orphans_removed'] == 1
      and d['bytes_freed'] == 5000 + 8000 + 2000 + 4096, d)
check('rows and log files removed', rows() == {running} and not any(exists(j) for j in finished)
      and not stray.exists())
check('the running job and unrelated files survive', exists(running)
      and pathlib.Path(f'{TMP}/jobs/notes.txt').exists())
check('fresh stats returned with the result', d['stats']['total'] == 1 and d['stats']['running'] == 1
      and d['stats']['log_files'] == 1)
aud = db.query("SELECT detail FROM audit WHERE action='jobs.cleanup' ORDER BY id DESC LIMIT 1", one=True)
check('audited with counts', aud and '3 job(s)' in aud['detail'] and '1 orphan log(s)' in aud['detail'], dict(aud) if aud else None)
n_aud = db.query("SELECT COUNT(*) AS c FROM audit WHERE action='jobs.cleanup'", one=True)['c']
r = admin.post('/api/jobs/cleanup', headers=ah, json={})
check('clearing an already-clean history deletes nothing and is not audited again',
      r.get_json()['deleted'] == 0 and r.get_json()['orphans_removed'] == 0
      and db.query("SELECT COUNT(*) AS c FROM audit WHERE action='jobs.cleanup'", one=True)['c'] == n_aud)

print('== automatic retention ==')
a = mkjob('shell', 'success', 10)
b = mkjob('shell', 'success', 2)
executor.JOB_RETENTION_DAYS = 3
lid = db.execute("INSERT INTO nodes (name,address,conn,ssh_user,ssh_port,groups,created_at,state) "
                 "VALUES ('local-x','localhost','local','root',22,'',?, 'managed')", (NOW,))
jid = executor.start_job('shell', 'x', {'node_ids': [lid], 'command': 'true'}, 'admin')
check('starting a job clears finished jobs older than the retention, keeps newer ones and the running one',
      a not in rows() and not exists(a) and b in rows() and running in rows() and jid in rows(), rows())
executor.JOB_RETENTION_DAYS = 0
check('stats reflect the runtime retention setting', admin.get('/api/jobs/stats').get_json()['retention_days'] == 0)

print('== page ==')
body = admin.get('/jobs').get_data(as_text=True)
check('summary and the single Clear all history button render for admin',
      'Clear all history' in body and 'logs:' in body and 'Clean up history' not in body)

print('== RBAC ==')
admin.post('/api/users', headers=ah, json={'username': 'op1', 'password': 'password123', 'role': 'operator'})
op, oh = client_for('op1', 'password123')
check('operator can read stats', op.get('/api/jobs/stats').status_code == 200)
check('operator cannot clear history', op.post('/api/jobs/cleanup', headers=oh, json={}).status_code == 403)
check('operator cannot delete a job', op.delete(f'/api/jobs/{b}', headers=oh).status_code == 403)
body = op.get('/jobs').get_data(as_text=True)
check('operator sees the summary but no clear button', 'logs:' in body and 'Clear all history' not in body)

print(f'\n{ok} passed, {fail} failed')
sys.exit(1 if fail else 0)
