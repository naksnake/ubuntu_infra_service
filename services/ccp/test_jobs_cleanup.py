"""Job history clean-up: stats, single delete (row + log file, running jobs
refused), bulk clean-up with filters and dry run, orphan log removal,
automatic retention, RBAC and the Jobs page.
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

def mkjob(kind, status, age_days, size=1000, by='admin'):
    """A job row plus a log file of `size` bytes, finished `age_days` ago."""
    created = NOW - age_days * DAY - 60
    finished = None if status == 'running' else created + 30
    jid = db.execute('INSERT INTO jobs (kind,target,spec,status,exit_code,created_by,created_at,finished_at) '
                     'VALUES (?,?,?,?,?,?,?,?)',
                     (kind, 'x', '{}', status, None if status == 'running' else (0 if status == 'success' else 1),
                      by, created, finished))
    pathlib.Path(executor._log_path(jid)).write_bytes(b'x' * size)
    return jid

def exists(jid):
    return os.path.exists(executor._log_path(jid))

def rows():
    return {r['id'] for r in db.query('SELECT id FROM jobs')}

admin, ah = client_for('admin', 'adminpass123')

# history: old and new, all kinds and states, one running
old_ok = [mkjob('shell', 'success', 30, 5000) for _ in range(3)]
old_bad = [mkjob('ansible', 'failed', 20, 8000) for _ in range(2)]
new_ok = [mkjob('shell', 'success', 1, 2000) for _ in range(2)]
new_bad = [mkjob('hwscan', 'failed', 0, 3000)]
running = mkjob('shell', 'running', 40, 100)
stray = pathlib.Path(f'{TMP}/jobs/999999.log')
stray.write_bytes(b'o' * 4096)
pathlib.Path(f'{TMP}/jobs/notes.txt').write_text('not a job log')

print('== stats ==')
r = admin.get('/api/jobs/stats')
s = r.get_json()
check('counts per status', r.status_code == 200 and s['total'] == 9 and s['running'] == 1
      and s['success'] == 5 and s['failed'] == 3, s)
check('log files and bytes on disk (stray .log counted, notes.txt not)',
      s['log_files'] == 10 and s['log_bytes'] == 3 * 5000 + 2 * 8000 + 2 * 2000 + 3000 + 100 + 4096, s)
check('retention reported off by default', s['retention_days'] == 0)

print('== dry run changes nothing ==')
before = rows()
r = admin.post('/api/jobs/cleanup', headers=ah,
               json={'status': 'finished', 'older_than_days': 7, 'dry_run': True})
d = r.get_json()
check('dry run reports the 5 old finished jobs + 1 orphan and the bytes',
      r.status_code == 200 and d['dry_run'] is True and d['deleted'] == 5 and d['orphans_removed'] == 1
      and d['bytes_freed'] == 3 * 5000 + 2 * 8000 + 4096, d)
check('nothing deleted, all logs still present', rows() == before and all(exists(j) for j in before)
      and stray.exists())
check('dry run is not audited',
      not any(a['action'] == 'jobs.cleanup' for a in db.query('SELECT action FROM audit')))

print('== filters ==')
r = admin.post('/api/jobs/cleanup', headers=ah,
               json={'status': 'failed', 'older_than_days': 7, 'orphans': False, 'dry_run': True})
check('failed-only + older-than picks the two old failed jobs', r.get_json()['deleted'] == 2
      and r.get_json()['orphans_removed'] == 0, r.get_json())
r = admin.post('/api/jobs/cleanup', headers=ah,
               json={'status': 'finished', 'older_than_days': 0, 'kinds': ['shell'], 'dry_run': True})
check('kind filter, any age: the five finished shell jobs (never the running one)',
      r.get_json()['deleted'] == 5, r.get_json())
r = admin.post('/api/jobs/cleanup', headers=ah,
               json={'status': 'finished', 'older_than_days': 0, 'keep_last': 6, 'dry_run': True})
check('keep_last spares the newest matching jobs', r.get_json()['deleted'] == 8 - 6, r.get_json())
for bad in ({'status': 'bogus'}, {'older_than_days': -1}, {'keep_last': 'many'},
            {'kinds': 'shell'}, {'kinds': ['rm -rf']}):
    r = admin.post('/api/jobs/cleanup', headers=ah, json=dict(bad, dry_run=True))
    check(f'rejected: {bad}', r.status_code == 400, r.get_json())

print('== the real thing ==')
r = admin.post('/api/jobs/cleanup', headers=ah,
               json={'status': 'finished', 'older_than_days': 7, 'keep_last': 1})
d = r.get_json()
check('deleted 4 (the newest old one kept) + the orphan',
      r.status_code == 200 and d['deleted'] == 4 and d['orphans_removed'] == 1 and d['dry_run'] is False, d)
survivors = rows()
kept_old = max(old_ok + old_bad)          # newest of the old ones by id
check('rows gone for the deleted jobs, kept_last survivor and all new jobs still there',
      kept_old in survivors and all(j in survivors for j in new_ok + new_bad + [running])
      and len(survivors) == 5, survivors)
check('log files removed with their rows', not any(exists(j) for j in (old_ok + old_bad) if j != kept_old)
      and exists(kept_old) and exists(running))
check('orphan log removed, unrelated file untouched', not stray.exists()
      and pathlib.Path(f'{TMP}/jobs/notes.txt').exists())
check('fresh stats returned with the result', d['stats']['total'] == 5 and d['stats']['running'] == 1)
aud = db.query("SELECT detail FROM audit WHERE action='jobs.cleanup' ORDER BY id DESC LIMIT 1", one=True)
check('audited with counts and filters', aud and '4 job(s)' in aud['detail'] and 'older_than=7d' in aud['detail']
      and 'keep_last=1' in aud['detail'], dict(aud) if aud else None)

print('== running jobs are untouchable ==')
r = admin.post('/api/jobs/cleanup', headers=ah, json={'status': 'finished', 'older_than_days': 0})
check('cleaning everything leaves the running job', running in rows() and exists(running)
      and len(rows()) == 1, rows())
r = admin.delete(f'/api/jobs/{running}', headers=ah)
check('single delete of a running job → 409', r.status_code == 409 and running in rows(), r.get_json())
db.execute("UPDATE jobs SET status='failed', finished_at=? WHERE id=?", (NOW, running))
r = admin.delete(f'/api/jobs/{running}', headers=ah)
check('single delete removes row AND log file once finished',
      r.status_code == 200 and running not in rows() and not exists(running))

print('== automatic retention ==')
a = mkjob('shell', 'success', 10)
b = mkjob('shell', 'success', 9)
c = mkjob('shell', 'success', 0)
executor.JOB_RETENTION_DAYS, executor.JOB_RETENTION_KEEP = 3, 1
lid = db.execute("INSERT INTO nodes (name,address,conn,ssh_user,ssh_port,groups,created_at,state) "
                 "VALUES ('local-x','localhost','local','root',22,'',?, 'managed')", (NOW,))
jid = executor.start_job('shell', 'x', {'node_ids': [lid], 'command': 'true'}, 'admin')
check('starting a job prunes finished jobs past the retention, keeping the newest one of them',
      a not in rows() and not exists(a) and b in rows() and c in rows() and jid in rows(), rows())
executor.JOB_RETENTION_DAYS, executor.JOB_RETENTION_KEEP = 0, 0
r = admin.get('/api/jobs/stats')
check('stats reflect the runtime retention setting', r.get_json()['retention_days'] == 0)

print('== page ==')
r = admin.get('/jobs')
body = r.get_data(as_text=True)
check('summary, clean-up controls and per-row delete render for admin',
      r.status_code == 200 and 'Clean up history' in body and 'retention:' in body
      and 'remove orphan log files' in body and 'cl-keep' in body, r.status_code)

print('== RBAC ==')
admin.post('/api/users', headers=ah, json={'username': 'op1', 'password': 'password123', 'role': 'operator'})
op, oh = client_for('op1', 'password123')
check('operator can read stats', op.get('/api/jobs/stats').status_code == 200)
check('operator cannot clean up', op.post('/api/jobs/cleanup', headers=oh, json={}).status_code == 403)
check('operator cannot delete a job', op.delete(f'/api/jobs/{c}', headers=oh).status_code == 403)
body = op.get('/jobs').get_data(as_text=True)
check('operator sees the summary but no clean-up controls',
      'retention:' in body and 'Clean up history' not in body)

print(f'\n{ok} passed, {fail} failed')
sys.exit(1 if fail else 0)
