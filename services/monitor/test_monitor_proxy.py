"""Prove the Lab Monitor proxies folder/move ops to a LIVE ipxe_manager.

Starts a real ipxe_manager on a port, stubs the 'docker' module so monitor
imports, points monitor at the live backend, logs in as admin, and drives
mkdir / move / rmdir through the monitor — asserting the effect lands in the
ipxe share and that the viewer role is refused.
"""
import os, sys, io, time, types, tempfile, pathlib, shutil, threading, wsgiref.simple_server
import importlib.util

def load(mod_name, path):
    """Load a module from an explicit path under a unique name — both services
    ship a file literally named app.py, so a plain `import app` would return
    whichever was imported first."""
    spec = importlib.util.spec_from_file_location(mod_name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = m
    spec.loader.exec_module(m)
    return m

TMP = tempfile.mkdtemp(prefix='mon_test_')
UP = pathlib.Path(TMP) / 'uploads'

# ── stub the 'docker' SDK so monitor/app.py imports without the real package ──
docker_stub = types.ModuleType('docker')
docker_stub.from_env = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('no docker in test'))
sys.modules['docker'] = docker_stub

# ── bring up a real ipxe_manager in-process on a background WSGI server ──
os.environ.update({
    'UPLOAD_DIR':   f'{TMP}/uploads',
    'ENTRIES_FILE': f'{TMP}/state/entries.json',
    'PROFILES_DIR': f'{TMP}/state/autoinstall',
    'AUTH_PASSWORD': 'mgr-secret',           # exercise the proxy's Basic auth
    'AUTH_USER': 'admin',
    # deliberately EMPTY: no folder is exempt by name, so a Lab Monitor upload
    # can only succeed via the explicit ?general=1 signal the proxy sends —
    # proving the fix doesn't depend on MONITOR_UPLOAD_DIR matching a list
    'IPXE_WHITELIST_EXEMPT_DIRS': '',
})
_HERE = os.path.dirname(os.path.abspath(__file__))
ipxe = load('ipxe_app', os.path.join(_HERE, '..', 'ipxe_manager', 'app.py'))
httpd = wsgiref.simple_server.make_server('127.0.0.1', 0, ipxe.app)
PORT = httpd.server_address[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()

# ── configure + import monitor pointed at the live backend ──
os.environ.update({
    'IPXE_MANAGER_URL':      f'http://127.0.0.1:{PORT}',
    'IPXE_MANAGER_USER':     'admin',
    'IPXE_MANAGER_PASSWORD': 'mgr-secret',
    'MONITOR_SECRET_KEY':     'test-key',
    'MONITOR_ADMIN_USER':     'admin',
    'MONITOR_ADMIN_PASSWORD': 'adminpass',
    'MONITOR_VIEWER_USER':    'bob',
    'MONITOR_VIEWER_PASSWORD': 'bobpass',
    # a name NOT in the backend's (empty) exempt list, so success proves the
    # explicit general-upload signal is what exempts the dashboard's space
    'MONITOR_UPLOAD_DIR':     'labfiles',
})
mon = load('monitor_app', os.path.join(_HERE, 'app.py'))

ok = fail = 0
def check(name, cond, detail=''):
    global ok, fail
    ok, fail = (ok + 1, fail) if cond else (ok, fail + 1)
    print(f'  {"PASS" if cond else "FAIL"}  {name}' + ('' if cond else f'   {detail}'))

def client(user, pw):
    c = mon.app.test_client()
    r = c.post('/login', data={'username': user, 'password': pw})
    assert r.status_code in (302, 303), f'{user} login {r.status_code}'
    with c.session_transaction() as s:
        csrf = s.get('csrf') or s.get('csrf_token') or ''
    return c, {'X-CSRF-Token': csrf} if csrf else {}

admin, ah = client('admin', 'adminpass')
bob, bh = client('bob', 'bobpass')

print('== monitor proxy: mkdir through to the live share ==')
r = admin.post('/api/folders', headers=ah, json={'name': 'netboot'})
check('mkdir via monitor -> 201', r.status_code == 201, f'{r.status_code} {r.data[:120]}')
check('folder exists in ipxe share', (UP / 'netboot').is_dir())

print('== whitelist scoping: Lab Monitor uploads are a GENERAL area ==')
# a non-boot file uploaded via the Lab Monitor lands in its own space and is
# accepted (this is the bug fix — previously it was wrongly rejected)
r = admin.post('/api/upload?subdir=docs', headers=ah,
               data={'file': (io.BytesIO(b'hello notes\n'), 'notes.txt')},
               content_type='multipart/form-data')
mdir = os.environ.get('MONITOR_UPLOAD_DIR', 'monitor')
check('general file via monitor accepted (201)', r.status_code == 201,
      f'{r.status_code} {r.data[:160]}')
check('notes.txt landed under monitor space', (UP / mdir / 'docs/notes.txt').is_file())
# a boot artifact via the monitor is of course fine too
r = admin.post('/api/upload?subdir=netboot', headers=ah,
               data={'file': (io.BytesIO(b'x' * 16), 'vmlinuz')},
               content_type='multipart/form-data')
check('boot file via monitor accepted (201)', r.status_code == 201, f'{r.status_code}')
check('vmlinuz landed under monitor space', (UP / mdir / 'netboot/vmlinuz').is_file())
# the whitelist STILL guards the netboot area on a DIRECT manager upload
r = ipxe.app.test_client().post(
    '/api/files',                       # direct to the owner, share root
    data={'file': (io.BytesIO(b'#!/bin/sh\n'), 'evil.sh')},
    content_type='multipart/form-data',
    headers={'Authorization': 'Basic ' +
             __import__('base64').b64encode(b'admin:mgr-secret').decode()})
check('direct netboot-root upload of evil.sh rejected (400)', r.status_code == 400,
      f'{r.status_code}')
check('evil.sh not at share root', not (UP / 'evil.sh').exists())
# and a DIRECT upload into the very same monitor folder, WITHOUT the general
# signal, is still whitelisted — so exemption comes from the signal, not the
# folder name (this is the fragility the fix removes)
r = ipxe.app.test_client().post(
    f'/api/files?dir={mdir}/docs',
    data={'file': (io.BytesIO(b'#!/bin/sh\n'), 'evil2.sh')},
    content_type='multipart/form-data',
    headers={'Authorization': 'Basic ' +
             __import__('base64').b64encode(b'admin:mgr-secret').decode()})
check('direct (non-general) upload into monitor dir still rejected (400)',
      r.status_code == 400, f'{r.status_code}')
check('evil2.sh not written', not (UP / mdir / 'docs/evil2.sh').exists())

print('== monitor proxy: move through to the live share ==')
r = admin.post('/api/files/move', headers=ah,
               json={'src': f'{mdir}/netboot/vmlinuz', 'dst': 'netboot/vmlinuz'})
check('move via monitor -> 200', r.status_code == 200, f'{r.status_code} {r.data[:120]}')
check('file moved in share', (UP / 'netboot/vmlinuz').is_file()
      and not (UP / mdir / 'netboot/vmlinuz').exists())

print('== monitor UI actions: rename (same-folder move) ==')
# the Rename button posts a move to the same dir with a new leaf name
r = admin.post('/api/files/move', headers=ah,
               json={'src': 'netboot/vmlinuz', 'dst': 'netboot/vmlinuz-6.8'})
check('rename via monitor -> 200', r.status_code == 200, f'{r.status_code}')
check('file renamed in share', (UP / 'netboot/vmlinuz-6.8').is_file()
      and not (UP / 'netboot/vmlinuz').exists())

print('== monitor UI actions: download whole folder as .zip ==')
r = admin.get('/api/download/folder/netboot', headers=ah)
check('folder .zip via monitor -> 200', r.status_code == 200, f'{r.status_code}')
check('response is a zip', r.data[:2] == b'PK'
      and 'zip' in r.headers.get('Content-Type', '').lower())

print('== monitor UI actions: download a single file ==')
r = admin.get('/api/download/file/netboot/vmlinuz-6.8', headers=ah)
check('file download via monitor -> 200', r.status_code == 200, f'{r.status_code}')
check('attachment disposition set',
      'attachment' in r.headers.get('Content-Disposition', '').lower())

print('== monitor UI actions: delete a single file ==')
r = admin.delete('/api/files/netboot/vmlinuz-6.8', headers=ah)
check('delete file via monitor -> 200', r.status_code == 200, f'{r.status_code}')
check('file gone from share', not (UP / 'netboot/vmlinuz-6.8').exists())

print('== monitor proxy: rmdir (empty vs recursive) ==')
# repopulate netboot via the BACKEND (a monitor upload would prefix mdir/),
# so the folder is genuinely non-empty for the empty-guard check
ipxe.app.test_client().post(
    '/api/files?dir=netboot',
    data={'file': (io.BytesIO(b'x' * 8), 'vmlinuz')},
    content_type='multipart/form-data',
    headers={'Authorization': 'Basic ' +
             __import__('base64').b64encode(b'admin:mgr-secret').decode()})
r = admin.delete('/api/folders/netboot', headers=ah)
check('rmdir non-empty without recursive -> 409', r.status_code == 409, f'{r.status_code}')
r = admin.delete('/api/folders/netboot?recursive=1', headers=ah)
check('rmdir recursive via monitor -> 200', r.status_code == 200, f'{r.status_code}')
check('folder gone from share', not (UP / 'netboot').exists())

print('== monitor proxy: viewer role is refused all mutations ==')
r = bob.post('/api/folders', headers=bh, json={'name': 'x'})
check('viewer mkdir -> 403', r.status_code == 403, f'{r.status_code}')
r = bob.post('/api/files/move', headers=bh, json={'src': 'a', 'dst': 'b'})
check('viewer move -> 403', r.status_code == 403, f'{r.status_code}')
r = bob.delete('/api/folders/anything', headers=bh)
check('viewer rmdir -> 403', r.status_code == 403, f'{r.status_code}')

httpd.shutdown()
print(f'\n{ok} passed, {fail} failed')
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if fail else 0)
