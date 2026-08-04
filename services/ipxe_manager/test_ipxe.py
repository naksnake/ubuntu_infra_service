"""ipxe_manager: whitelist + folder/move ops + traversal, via Flask test client."""
import os, sys, io, json, tempfile, pathlib, shutil

TMP = tempfile.mkdtemp(prefix='ipxe_test_')
os.environ.update({
    'UPLOAD_DIR':   f'{TMP}/uploads',
    'ENTRIES_FILE': f'{TMP}/state/entries.json',
    'PROFILES_DIR': f'{TMP}/state/autoinstall',
    'AUTH_PASSWORD': '',            # auth disabled for the harness
})
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as ipxe                                            # noqa: E402

UP = pathlib.Path(TMP) / 'uploads'
c = ipxe.app.test_client()
ok = fail = 0

def check(name, cond, detail=''):
    global ok, fail
    ok, fail = (ok + 1, fail) if cond else (ok, fail + 1)
    print(f'  {"PASS" if cond else "FAIL"}  {name}' + ('' if cond else f'   {detail}'))

def up(name, dir=None):
    q = f'?dir={dir}' if dir else ''
    return c.post(f'/api/files{q}',
                  data={'file': (io.BytesIO(b'x' * 32), name)},
                  content_type='multipart/form-data')

print('== whitelist: allowed names ==')
for n in ['ubuntu-24.04.iso', 'UBUNTU.ISO', 'initrd.gz', 'rootfs.cpio.gz',
          'rootfs.cpio', 'vmlinuz', 'vmlinuz-6.8.0-40-generic', 'vmlinuz.efi',
          'rootfs', 'rootfs.squashfs',
          # substring anywhere in the name: vendor / build-tagged artifacts
          'gb200_06v_vmlinuz', 'nvidia_vmlinuz_v2', 'GB200_06V_VMLINUZ',
          'custom-rootfs-image', 'node1.rootfs.img', 'a_vmlinuz_b']:
    r = up(n)
    check(f'accept {n!r:32}', r.status_code == 201, f'{r.status_code} {r.get_json()}')

print('== whitelist: rejected names ==')
# note: names CONTAINING vmlinuz/rootfs are intentionally allowed now, so a
# rejection case must contain neither token and end in no allowed extension
for n in ['evil.sh', 'notes.txt', 'payload.php', 'archive.zip', 'a.exe',
          'config.yaml', 'image.qcow2', 'kernel.tar', 'x.gz.sh', 'initramfs.xz']:
    r = up(n)
    check(f'reject {n!r:32}', r.status_code == 400, f'{r.status_code}')
    check(f'  …{n!r:30} not written', not (UP / n).exists())
check('no .spool-* temp files leaked', not list(UP.glob('.spool-*')))

print('== names preserved EXACTLY (no secure_filename mangling) ==')
# each of these would be renamed by secure_filename (spaces->_, unicode dropped,
# leading dots stripped, case folded). They must land byte-for-byte, in the
# folder they were uploaded to, under the Lab Monitor space (whitelist-exempt).
exact = [
    ('My Report (final).PDF', 'monitor'),
    ('Résumé — 2026.txt',      'monitor/Curriculum Vitæ'),
    ('DATA SET v1.2.csv',      'monitor/Q3 Reports'),
    ('螺旋.iso',                'monitor'),
    ('a & b, c.txt',           'monitor/odd names'),
]
for fname, d in exact:
    r = up(fname, d)
    landed = UP / d / fname
    check(f'accept & keep exact name {fname!r}', r.status_code == 201, f'{r.status_code} {r.get_json()}')
    check(f'  on disk verbatim under {d!r}', landed.is_file(),
          f'expected {landed}')
    # the API reports the exact relative name back
    if r.status_code == 201:
        check(f'  API returns exact name', r.get_json().get('name') == f'{d}/{fname}',
              r.get_json().get('name'))
# a preserved-name file is retrievable, movable and deletable by its exact path
src = 'monitor/Q3 Reports/DATA SET v1.2.csv'
from urllib.parse import quote as _q
r = c.get('/api/files/download/' + '/'.join(_q(p) for p in src.split('/')))
check('download by exact (encoded) path', r.status_code == 200, f'{r.status_code}')
r = c.post('/api/files/move', json={'src': src, 'dst': 'monitor/Q3 Reports/DATA SET v1.3.csv'})
check('rename preserves spaces & dots', r.status_code == 200
      and (UP / 'monitor/Q3 Reports/DATA SET v1.3.csv').is_file()
      and not (UP / 'monitor/Q3 Reports/DATA SET v1.2.csv').exists(), f'{r.status_code}')

print('== traversal STILL blocked with the preserving validator ==')
for fname, d in [('x.iso', '../escape'), ('x.iso', 'a/../../etc'),
                 ('x.iso', '/etc'), ('x.iso', 'a/./b')]:
    r = up(fname, d)
    # '/etc' and 'a/./b' are contained/normalized (safe); '..' variants rejected
    if '..' in d:
        check(f'reject dir={d!r}', r.status_code == 400, f'{r.status_code}')
check('nothing escaped the share', not (UP.parent / 'escape').exists()
      and not pathlib.Path('/etc/x.iso').exists())
# NUL and control chars in a name are rejected
r = up('bad\x00name.iso', 'monitor')
check('NUL in filename rejected', r.status_code == 400, f'{r.status_code}')

print('== whitelist scoping: monitor space is exempt, netboot is not ==')
# general (non-boot) files are REJECTED at the netboot root / arbitrary dirs …
for d in [None, 'images', 'boot/x']:
    r = up('notes.txt', d)
    check(f'reject notes.txt in netboot dir={d!r}', r.status_code == 400,
          f'{r.status_code}')
# … but ACCEPTED inside the Lab Monitor's own space (default exempt 'monitor')
for path, d in [('notes.txt', 'monitor'), ('report.pdf', 'monitor/docs'),
                ('evil.sh', 'monitor'), ('data.csv', 'monitor/2026/q3')]:
    r = up(path, d)
    check(f'accept {path!r} in monitor dir={d!r}', r.status_code == 201,
          f'{r.status_code} {r.get_json()}')
    check(f'  …{path!r} written under monitor/', (UP / d / path).is_file())
# boot artifacts are still fine everywhere, including the exempt space
check('boot file still ok in monitor space',
      up('vmlinuz', 'monitor').status_code == 201)

print('== folders: mkdir / nested / duplicate ==')
r = c.post('/api/folders', json={'name': 'images'})
check('mkdir images', r.status_code == 201 and (UP / 'images').is_dir())
r = c.post('/api/folders', json={'dir': 'images', 'name': 'ubuntu'})
check('mkdir images/ubuntu', r.status_code == 201 and (UP / 'images/ubuntu').is_dir())
r = c.post('/api/folders', json={'name': 'images'})
check('mkdir duplicate -> 409', r.status_code == 409)
for bad in [{'name': '../evil'}, {'name': 'a/b'}, {'dir': '../..', 'name': 'x'},
            {'name': ''}]:
    r = c.post('/api/folders', json=bad)
    check(f'mkdir {json.dumps(bad):40} rejected', r.status_code == 400,
          f'{r.status_code}')
# an absolute-looking path is CONTAINED inside the share, never obeyed
r = c.post('/api/folders', json={'dir': '/etc', 'name': 'x'})
check('mkdir dir=/etc contained inside share', r.status_code == 201
      and (UP / 'etc/x').is_dir() and not pathlib.Path('/etc/x').exists())
check('no traversal folder escaped', not (UP.parent / 'evil').exists())

print('== move / rename file ==')
up('vmlinuz-6.8.0-40-generic')
r = c.post('/api/files/move', json={'src': 'vmlinuz-6.8.0-40-generic',
                                    'dst': 'images/ubuntu/vmlinuz'})
check('move file into subfolder', r.status_code == 200
      and (UP / 'images/ubuntu/vmlinuz').is_file()
      and not (UP / 'vmlinuz-6.8.0-40-generic').exists())
r = c.post('/api/files/move', json={'src': 'images/ubuntu/vmlinuz',
                                    'dst': 'images/ubuntu/vmlinuz'})
check('move onto itself -> 400', r.status_code == 400)
r = c.post('/api/files/move', json={'src': 'nope.iso', 'dst': 'x.iso'})
check('move missing src -> 404', r.status_code == 404)
up('rootfs.cpio.gz')
r = c.post('/api/files/move', json={'src': 'rootfs.cpio.gz',
                                    'dst': 'images/ubuntu/vmlinuz'})
check('move onto existing dst -> 409', r.status_code == 409)
for bad in [{'src': '../etc/passwd', 'dst': 'x.iso'},
            {'src': 'rootfs.cpio.gz', 'dst': '../escape.iso'}]:
    r = c.post('/api/files/move', json=bad)
    check(f'move {json.dumps(bad):48} rejected', r.status_code in (400, 404),
          f'{r.status_code}')
# absolute dst is CONTAINED, not obeyed: lands under the share, real /etc safe
r = c.post('/api/files/move', json={'src': 'rootfs.cpio.gz', 'dst': '/etc/cron.d/x'})
check('move dst=/etc/... contained inside share', r.status_code == 200
      and (UP / 'etc/cron.d/x').is_file()
      and not pathlib.Path('/etc/cron.d/x').exists())
check('nothing escaped via move', not (UP.parent / 'escape.iso').exists())

print('== move folder + boot-entry path rewrite ==')
ipxe.save_entries([{'id': 'e1', 'type': 'kernel',
                    'kernel': 'images/ubuntu/vmlinuz',
                    'initrd': 'images/ubuntu/initrd', 'enabled': True}])
r = c.post('/api/files/move', json={'src': 'images/ubuntu', 'dst': 'images/noble'})
ents = ipxe.load_entries()
check('folder moved', r.status_code == 200 and (UP / 'images/noble/vmlinuz').is_file())
check('entry kernel path rewritten', ents[0]['kernel'] == 'images/noble/vmlinuz')
check('entry initrd path rewritten', ents[0]['initrd'] == 'images/noble/initrd')
r = c.post('/api/files/move', json={'src': 'images', 'dst': 'images/deeper'})
check('move folder into own subtree -> 400', r.status_code == 400)

print('== rmdir: empty-only vs recursive + dangling-entry prune ==')
c.post('/api/folders', json={'name': 'empty'})
r = c.delete('/api/folders/empty')
check('rmdir empty folder', r.status_code == 200 and not (UP / 'empty').exists())
r = c.delete('/api/folders/images')
check('rmdir non-empty without recursive -> 409', r.status_code == 409
      and (UP / 'images').is_dir())
r = c.delete('/api/folders/images?recursive=1')
check('rmdir recursive', r.status_code == 200 and not (UP / 'images').exists())
check('dangling kernel entry pruned', ipxe.load_entries() == [])
r = c.delete('/api/folders/../uploads')
check('rmdir traversal rejected', r.status_code in (400, 404))

print('== ISO auto-extraction path still whitelisted-compatible ==')
r = up('ubuntu-24.04.iso')       # accepted; extraction is a no-op w/o pycdlib
check('ISO upload still accepted', r.status_code == 201)

print(f'\n{ok} passed, {fail} failed')
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if fail else 0)
