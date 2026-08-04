"""Exercise the per-user file storage with Flask's test client.

Covers: auto-created user roots, subfolder upload, isolation between users,
a path-traversal battery, symlink escape, filename attacks, quota, username
validation, and archive-on-user-delete.
"""
import os, sys, tempfile, pathlib, shutil

TMP = tempfile.mkdtemp(prefix='ccp_test_')
os.environ.update({
    'CCP_DB': f'{TMP}/ccp.db',
    'CCP_FILES_DIR': f'{TMP}/files',
    'CCP_JOBS_DIR': f'{TMP}/jobs',
    'CCP_ADMIN_USER': 'admin',
    'CCP_ADMIN_PASSWORD': 'adminpass123',
    'CCP_USER_QUOTA_MB': '10240',
})
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as ccp                                            # noqa: E402

FILES = pathlib.Path(TMP) / 'files'
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

def up(c, h, folder, name, data=b'hello world\n'):
    return c.post('/api/files', headers=h,
                  data={'folder': folder, 'file': (__import__('io').BytesIO(data), name)},
                  content_type='multipart/form-data')

print('== setup: admin creates rex and john ==')
admin, ah = client_for('admin', 'adminpass123')
for u in ('rex', 'john'):
    r = admin.post('/api/users', headers=ah, json={'username': u, 'password': 'password123'})
    assert r.status_code == 201, r.get_json()

rex, rh = client_for('rex', 'password123')
john, jh = client_for('john', 'password123')

print('== requirement: root auto-created, upload to root and subfolder ==')
r = rex.get('/files')
check('visiting /files auto-creates /files/rex', (FILES / 'rex').is_dir())
r = up(rex, rh, '', 'notes.txt')
check('upload to own root', r.status_code == 201, r.get_json())
r = up(rex, rh, 'docs/2026', 'plan.txt', b'secret plan')
check('upload auto-creates selected subfolder', r.status_code == 201
      and (FILES / 'rex/docs/2026/plan.txt').read_bytes() == b'secret plan')
r = rex.post('/api/files/mkdir', headers=rh, json={'folder': 'docs', 'name': 'img'})
check('mkdir inside subfolder', r.status_code == 201 and (FILES / 'rex/docs/img').is_dir())
r = rex.get('/files/download/docs/2026/plan.txt')
check('download own nested file', r.status_code == 200 and r.data == b'secret plan')
r = rex.get('/files?folder=docs')
check('listing shows subfolder contents', b'2026' in r.data and b'img' in r.data)

print('== requirement: isolation — john cannot touch rex ==')
r = john.get('/files/download/notes.txt')
check('john cannot download rex root file', r.status_code == 404)
r = john.get('/files/download/docs/2026/plan.txt')
check('john cannot download rex nested file', r.status_code == 404)
r = john.delete('/api/files/docs/2026/plan.txt', headers=jh)
check('john cannot delete rex file', r.status_code == 404
      and (FILES / 'rex/docs/2026/plan.txt').exists())
r = up(john, jh, '', 'notes.txt', b'johns own')
check('same filename lands in johns space', r.status_code == 201
      and (FILES / 'john/notes.txt').read_bytes() == b'johns own'
      and (FILES / 'rex/notes.txt').read_bytes() == b'hello world\n')

print('== path traversal battery ==')
attacks_folder = ['../john', '..', 'a/../../john', '/etc', 'a/./b',
                  '..\\john', 'a\\..\\b', '....//john', 'a/%2e%2e/b',
                  '.ssh', 'a/.hidden/b', 'a//b', '\x00etc', 'a' * 600,
                  'a/b/c/d/e/f/g/h/i']
for atk in attacks_folder:
    r = up(rex, rh, atk, 'x.txt')
    check(f'upload folder={atk!r:.30} rejected', r.status_code in (400, 403),
          f'got {r.status_code}')
check('no stray file escaped to john', not (FILES / 'john/x.txt').exists())
check('no stray file escaped above root', not (FILES.parent / 'x.txt').exists()
      and not pathlib.Path('/etc/x.txt').exists())

for atk in ['../john/notes.txt', '..%2fjohn%2fnotes.txt', '%2e%2e/john/notes.txt',
            '..%5cjohn%5cnotes.txt', 'docs/../../john/notes.txt', '.ssh/id_rsa']:
    r = rex.get(f'/files/download/{atk}')
    check(f'download {atk!r:.40} rejected', r.status_code in (400, 403, 404),
          f'got {r.status_code}')

r = rex.delete('/api/files/../john/notes.txt', headers=rh)
check('delete traversal rejected', r.status_code in (400, 403, 404)
      and (FILES / 'john/notes.txt').exists())
r = rex.post('/api/files/mkdir', headers=rh, json={'folder': '', 'name': '../pwn'})
check('mkdir traversal name rejected', r.status_code == 400
      and not (FILES / 'pwn').exists())
r = rex.post('/api/files/mkdir', headers=rh,
             json={'folder': 'a/b/c/d/e/f/g/h', 'name': 'i'})
check('mkdir beyond depth limit rejected', r.status_code == 400)

print('== symlink escape (attacker with fs access plants a link) ==')
evil = FILES / 'rex/evil'
evil.symlink_to('/etc')
r = rex.get('/files/download/evil/hostname')
check('download through symlink dir blocked', r.status_code in (403, 404),
      f'got {r.status_code}')
(FILES / 'rex/leak').symlink_to('/etc/hostname')
r = rex.get('/files/download/leak')
check('download of symlink file blocked', r.status_code in (403, 404))
r = rex.get('/files')
check('symlinks hidden from listing', b'evil' not in r.data and b'leak' not in r.data)
evil.unlink(); (FILES / 'rex/leak').unlink()

print('== filename attacks ==')
r = up(rex, rh, '', '../../etc/cron.d/evil')
check('traversal filename neutralized (secure_filename)',
      r.status_code in (201, 400) and not pathlib.Path('/etc/cron.d/evil').exists()
      and not (FILES / 'etc').exists())
r = up(rex, rh, '', '.bashrc')
check('hidden filename neutralized (stored non-hidden or rejected)',
      (r.status_code == 400) or
      (r.status_code == 201 and not r.get_json()['name'].startswith('.')
       and not (FILES / 'rex/.bashrc').exists()))
r = up(rex, rh, '', '-rf')
check('leading-dash filename rejected', r.status_code == 400)
r = up(rex, rh, '', 'x' * 100 + '.txt')
check('overlong filename rejected', r.status_code == 400)

print('== quota ==')
r = admin.post('/api/users', headers=ah,
               json={'username': 'tiny', 'password': 'password123', 'quota_mb': 1})
check('create user with 1 MB quota', r.status_code == 201, r.get_json())
tiny, th = client_for('tiny', 'password123')
r = up(tiny, th, '', 'small.bin', b'x' * (700 * 1024))
check('upload under quota accepted', r.status_code == 201)
r = up(tiny, th, '', 'big.bin', b'x' * (700 * 1024))
check('upload over quota rejected 413', r.status_code == 413
      and not (FILES / 'tiny/big.bin').exists())
check('no .part temp left behind',
      not list((FILES / 'tiny').glob('.*part')))
r = up(tiny, th, '', 'small.bin', b'y' * (800 * 1024))
check('replacing a file counts old size freed', r.status_code == 201)

print('== username validation & user lifecycle ==')
for bad in ['../evil', 'a b', '.hidden', 'x/y', 'a' * 40, 'ünïcode']:
    r = admin.post('/api/users', headers=ah,
                   json={'username': bad, 'password': 'password123'})
    check(f'username {bad!r:.20} rejected', r.status_code == 400)
uid = ccp.db.query('SELECT id FROM users WHERE username=?', ('tiny',), one=True)['id']
r = admin.delete(f'/api/users/{uid}', headers=ah)
archived = list(FILES.glob('_removed-tiny-*'))
check('user delete archives their directory', r.status_code == 200
      and not (FILES / 'tiny').exists() and len(archived) == 1
      and (archived[0] / 'small.bin').exists())

print('== move / rename within own space ==')
up(rex, rh, '', 'movable.txt', b'move me')
r = rex.post('/api/files/move', headers=rh,
             json={'src': 'movable.txt', 'dst': 'docs/moved.txt'})
check('rename/move file into subfolder', r.status_code == 200
      and (FILES / 'rex/docs/moved.txt').read_bytes() == b'move me'
      and not (FILES / 'rex/movable.txt').exists())
r = rex.get('/files/download/docs/moved.txt')
check('moved file downloadable at new path', r.status_code == 200 and r.data == b'move me')
r = rex.post('/api/files/move', headers=rh,
             json={'src': 'docs', 'dst': 'documents'})
check('move (rename) a whole folder', r.status_code == 200
      and (FILES / 'rex/documents/moved.txt').is_file()
      and not (FILES / 'rex/docs').exists())
r = rex.post('/api/files/move', headers=rh,
             json={'src': 'documents', 'dst': 'documents/inner'})
check('move folder into itself -> 400', r.status_code == 400)
r = rex.post('/api/files/move', headers=rh,
             json={'src': '../john/notes.txt', 'dst': 'stolen.txt'})
check('move with traversal src rejected', r.status_code in (400, 403, 404)
      and not (FILES / 'rex/stolen.txt').exists())
r = rex.post('/api/files/move', headers=rh,
             json={'src': 'documents/moved.txt', 'dst': '../john/planted.txt'})
check('move dst traversal rejected, john untouched', r.status_code in (400, 403)
      and not (FILES / 'john/planted.txt').exists())

print('== auth & csrf still enforced ==')
anon = ccp.app.test_client()
check('anonymous /api/files denied', anon.post('/api/files').status_code == 401)
check('anonymous download denied',
      anon.get('/files/download/notes.txt').status_code in (302, 401))
r = rex.post('/api/files', data={'folder': '', 'file': (__import__('io').BytesIO(b'x'), 'c.txt')},
             content_type='multipart/form-data')   # no CSRF header
check('upload without CSRF token rejected', r.status_code == 403)

print(f'\n{ok} passed, {fail} failed')
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if fail else 0)
