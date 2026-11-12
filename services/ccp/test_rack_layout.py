"""Rack layout file tests: Excel / CSV / JSON / YAML parsing and validation,
the import API (preview, replace, merge, clear, RBAC, audit), precedence over
hostname topology, expected slots in the rack view, planned hostnames on
Discovery import, and export/template round-trips.
"""
import io, os, sys, time, json, tempfile, pathlib

TMP = tempfile.mkdtemp(prefix='ccp_layout_')
LEASES = f'{TMP}/dnsmasq.leases'
os.environ.update({
    'CCP_DB': f'{TMP}/ccp.db',
    'CCP_FILES_DIR': f'{TMP}/files',
    'CCP_JOBS_DIR': f'{TMP}/jobs',
    'CCP_SSH_KEY': f'{TMP}/ssh/id_ccp',
    'CCP_LEASES_FILE': LEASES,
    'CCP_ADMIN_USER': 'admin',
    'CCP_ADMIN_PASSWORD': 'adminpass123',
    'CCP_TEST_SYNC_JOBS': '1',
})
pathlib.Path(f'{TMP}/ssh').mkdir(parents=True)
pathlib.Path(f'{TMP}/ssh/id_ccp').write_text('FAKE\n')
pathlib.Path(f'{TMP}/ssh/id_ccp.pub').write_text('ssh-ed25519 AAAATEST ccp\n')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import openpyxl                                              # noqa: E402
import app as ccp                                            # noqa: E402
import db                                                    # noqa: E402
import rack_layout as rl                                     # noqa: E402

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

def xlsx(rows, header_offset=0, title='Sheet1'):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = title
    for _ in range(header_offset):
        ws.append(['Lab A — rack plan'])
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()

def node(name):
    return db.query('SELECT * FROM nodes WHERE name=?', (name,), one=True)

def add_node(name, address, mac='', state='managed'):
    nid = db.execute(
        'INSERT INTO nodes (name,address,conn,ssh_user,ssh_port,groups,mac,state,created_at) '
        "VALUES (?,?,'ssh','root',22,'',?,?,?)", (name, address, mac, state, int(time.time())))
    ccp.topology.apply(nid, name)
    return nid

print('== value normalisation ==')
for raw, want in (('00-1a-2b-3c-4d-5e', '00:1A:2B:3C:4D:5E'),
                  ('001a.2b3c.4d5e', '00:1A:2B:3C:4D:5E'),
                  ('001A2B3C4D5E', '00:1A:2B:3C:4D:5E'), ('', ''), ('zz:zz', None)):
    check(f'norm_mac {raw!r}', rl.norm_mac(raw) == want, rl.norm_mac(raw))
for raw, want in ((3, 3), (3.0, 3), ('3', 3), ('rack3', 3), ('Rack 03', 3),
                  ('R03', 3), ('rack-12', 12)):
    check(f'rack {raw!r} → {want}', rl._number(raw, 'rack') == want)
for raw, want in (('sled2', 2), ('U12', 12), ('slot 4', 4)):
    check(f'sled {raw!r} → {want}', rl._number(raw, 'sled') == want)
for bad in ('row3', 2.5, True, -1, 'three'):
    try:
        rl._number(bad, 'rack')
        check(f'rack {bad!r} rejected', False)
    except ValueError:
        check(f'rack {bad!r} rejected', True)

print('== Excel ==')
data = xlsx([['Host Name', 'MAC Address', 'IP Address', 'Rack', 'Slot', 'Type', 'Serial'],
             ['rack0-sled1-gpu', '00-1a-2b-3c-4d-01', '192.168.100.21', 'Rack 0', 1, 'GPU', 'SN1'],
             [None, None, None, None, None, None, None],
             [None, '00:1a:2b:3c:4d:02', None, 0.0, 2.0, 'gpu', 'SN2']],
            header_offset=2)
rows, errs, warns = rl.parse('plan.xlsx', data)
check('xlsx parses with title rows above the header and a blank row', not errs and len(rows) == 2,
      (rows, errs))
check('xlsx header aliases + normalisation', rows[0]['hostname'] == 'rack0-sled1-gpu'
      and rows[0]['mac'] == '00:1A:2B:3C:4D:01' and rows[0]['rack'] == 0
      and rows[0]['sled'] == 1 and rows[0]['role'] == 'gpu', rows[0])
check('xlsx float cells become ints', rows[1]['rack'] == 0 and rows[1]['sled'] == 2, rows[1])
check('xlsx error labels use the sheet row number', rows[1]['label'] == 'row 6', rows[1])
check('unknown columns warned, not fatal', warns and 'Serial' in warns[0], warns)
named = xlsx([['notes'], ['ignore me']], title='Cover')
wb = openpyxl.load_workbook(io.BytesIO(named))
ws2 = wb.create_sheet('layout')
ws2.append(['mac', 'rack'])
ws2.append(['00:1a:2b:3c:4d:09', 4])
buf = io.BytesIO(); wb.save(buf)
rows, errs, _ = rl.parse('plan.xlsx', buf.getvalue())
check('a sheet named "layout" is preferred', not errs and rows[0]['rack'] == 4, (rows, errs))
try:
    rl.parse('plan.xlsx', b'not a zip')
    check('corrupt xlsx → LayoutError', False)
except rl.LayoutError as e:
    check('corrupt xlsx → LayoutError', 'xlsx' in str(e), e)
try:
    rl.parse('plan.xlsx', xlsx([['foo', 'bar'], [1, 2]]))
    check('xlsx without header → LayoutError', False)
except rl.LayoutError as e:
    check('xlsx without header → LayoutError', 'header' in str(e), e)
for fn, msg in (('old.xls', '.xlsx'), ('plan.txt', 'unsupported')):
    try:
        rl.parse(fn, b'x')
        check(f'{fn} rejected', False)
    except rl.LayoutError as e:
        check(f'{fn} rejected', msg in str(e), e)

print('== CSV / JSON / YAML ==')
rows, errs, _ = rl.parse('p.csv', '﻿hostname,mac,rack,sled\nn1,00:00:00:00:00:01,1,1\n,,,\n'.encode())
check('csv with BOM, blank row skipped', not errs and len(rows) == 1 and rows[0]['rack'] == 1,
      (rows, errs))
rows, errs, _ = rl.parse('p.json', json.dumps(
    [{'hostname': 'n1', 'rack': 1}, {'ip': '10.0.0.5', 'rack': 'rack2', 'sled': 'sled3'}]).encode())
check('json flat list', not errs and rows[1]['address'] == '10.0.0.5' and rows[1]['sled'] == 3,
      (rows, errs))
rows, errs, _ = rl.parse('p.json', json.dumps({'nodes': [{'hostname': 'n1', 'rack': 1}]}).encode())
check('json {"nodes": [...]}', not errs and len(rows) == 1, errs)
rows, errs, _ = rl.parse('p.yaml', b'''
racks:
  - rack: 3
    nodes:
      - {sled: 1, hostname: a1, role: GPU}
      - {sled: 2, mac: "00:00:00:00:00:02", rack: 9}
''')
check('yaml racks list: rack inherited, row may override', not errs
      and rows[0]['rack'] == 3 and rows[0]['role'] == 'gpu' and rows[1]['rack'] == 9, (rows, errs))
rows, errs, _ = rl.parse('p.yml', b'racks:\n  rack5:\n    - {sled: 1, hostname: b1}\n')
check('yaml racks mapping keyed "rack5"', not errs and rows[0]['rack'] == 5, (rows, errs))
for fn, body, msg in (('p.json', b'{bad', 'JSON'), ('p.yaml', b'a: [', 'YAML'),
                      ('p.yaml', b'', 'empty'), ('p.json', b'{"x": 1}', 'nodes')):
    try:
        rl.parse(fn, body)
        check(f'{fn} {body!r} rejected', False)
    except rl.LayoutError as e:
        check(f'{fn} {body!r} rejected', msg in str(e), e)

print('== row validation and duplicates ==')
rows, errs, _ = rl.parse('p.json', json.dumps([
    {'hostname': 'ok1', 'rack': 1, 'sled': 1},
    {'hostname': 'no-rack'},
    {'rack': 1, 'role': 'gpu'},
    {'hostname': 'bad name', 'rack': 1},
    {'mac': 'nope', 'rack': 1},
    {'hostname': 'OK1', 'rack': 2},
    {'hostname': 'ok2', 'rack': 1, 'sled': 1},
    'just a string',
]).encode())
joined = '\n'.join(errs)
check('missing rack reported', 'item 2: rack is required' in joined, joined)
check('row without identifier reported', 'item 3' in joined and 'at least one' in joined, joined)
check('invalid hostname reported', 'item 4' in joined, joined)
check('invalid mac reported', 'item 5' in joined and 'MAC' in joined, joined)
check('duplicate hostname (case-insensitive) reported', 'item 6: hostname' in joined, joined)
check('two machines in one slot reported', 'item 7: slot rack 1 sled 1' in joined, joined)
check('non-mapping row reported', 'item 8' in joined, joined)

print('== import API ==')
admin, ah = client_for('admin', 'adminpass123')
admin.post('/api/users', headers=ah,
           json={'username': 'viewer1', 'password': 'password123', 'role': 'viewer'})
viewer, vh = client_for('viewer1', 'password123')

a = add_node('rack0_sled5_gpu', '192.168.100.30', mac='00:1A:2B:3C:4D:01')
b = add_node('web-01', '192.168.100.31')
check('before import: hostname topology', node('rack0_sled5_gpu')['rack'] == 0
      and node('rack0_sled5_gpu')['topo_source'] == 'hostname')

def upload(client, headers, fn, body, **form):
    form['file'] = (io.BytesIO(body), fn)
    return client.post('/api/rack/layout', headers=headers, data=form,
                       content_type='multipart/form-data')

plan = xlsx([['hostname', 'mac', 'ip', 'rack', 'sled', 'role'],
             ['rack2-sled1-gpu', '00:1a:2b:3c:4d:01', '', 2, 1, 'gpu'],
             ['', '', '192.168.100.31', 3, 4, 'cpu'],
             ['rack2-sled2-gpu', '00:1a:2b:3c:4d:02', '', 2, 2, 'gpu']])
r = upload(viewer, vh, 'plan.xlsx', plan)
check('viewer cannot import a layout', r.status_code == 403)
r = upload(admin, ah, 'plan.xlsx', plan, dry_run='1')
d = r.get_json()
check('dry run → 200 preview', r.status_code == 200 and d['summary'] ==
      {'rows': 3, 'matched': 2, 'racks': 2}, d)
check('dry run stores nothing', rl.meta()['rows'] == 0)
check('preview shows the move and planned rename', d['rows'][0]['node'] == 'rack0_sled5_gpu'
      and d['rows'][0]['moves_from'] == 'rack 0 sled 5'
      and d['rows'][0]['rename'] == 'rack2-sled1-gpu', d['rows'][0])
bad = xlsx([['hostname', 'rack'], ['x1', 1], ['x2', None]])
r = upload(admin, ah, 'bad.xlsx', bad, dry_run='1')
check('dry run with errors still previews (200)', r.status_code == 200
      and r.get_json()['error_count'] == 1, r.get_json())
r = upload(admin, ah, 'bad.xlsx', bad)
check('apply with errors → 400 and nothing stored', r.status_code == 400
      and rl.meta()['rows'] == 0, r.get_json())
r = upload(admin, ah, 'x.pdf', b'%PDF')
check('unsupported type → 400', r.status_code == 400 and 'unsupported' in r.get_json()['error'])
r = admin.post('/api/rack/layout', headers=ah, data={}, content_type='multipart/form-data')
check('no file → 400', r.status_code == 400)
r = upload(admin, ah, 'plan.xlsx', plan, mode='bogus')
check('bad mode → 400', r.status_code == 400)

r = upload(admin, ah, 'plan.xlsx', plan)
d = r.get_json()
check('apply → 201, 2 nodes placed', r.status_code == 201 and d['placed'] == 2, d)
na, nb = node('rack0_sled5_gpu'), node('web-01')
check('layout (by MAC) overrides the hostname topology', na['rack'] == 2 and na['sled'] == 1
      and na['topo_source'] == 'layout', dict(na))
check('node with a non-rack name placed by IP', nb['rack'] == 3 and nb['sled'] == 4
      and nb['role'] == 'cpu' and nb['topo_source'] == 'layout', dict(nb))
check('meta records source + user', rl.meta()['source'] == 'plan.xlsx'
      and rl.meta()['imported_by'] == 'admin', rl.meta())
check('import audited', db.query("SELECT 1 FROM audit WHERE action='rack.layout.import'", one=True))

print('== rack view ==')
html = viewer.get('/rack').get_data(as_text=True)
check('rack view lists layout racks', 'Rack 2' in html and 'Rack 3' in html)
check('unmatched layout row is an expected slot', 'sled-expected' in html
      and 'rack2-sled2-gpu' in html, html[:0])
check('planned-name hint for a matched node', 'planned name' in html)
check('viewer sees no import controls', 'id="ly-file"' not in html)
check('operator sees import controls', 'id="ly-file"' in admin.get('/rack').get_data(as_text=True))

print('== later node changes respect the layout ==')
c = add_node('whatever', '192.168.100.50', mac='00-1A-2B-3C-4D-02')
check('a node added later with a listed MAC is placed', node('whatever')['rack'] == 2
      and node('whatever')['sled'] == 2, dict(node('whatever')))

print('== merge / export / clear ==')
r = upload(admin, ah, 'more.yaml', b'nodes:\n  - {ip: 192.168.100.31, rack: 7, sled: 1}\n'
           b'  - {hostname: extra1, rack: 8}\n', mode='merge')
check('merge → 201', r.status_code == 201, r.get_json())
check('merge replaced the conflicting row, kept the rest', rl.meta()['rows'] == 4
      and node('web-01')['rack'] == 7 and node('rack0_sled5_gpu')['rack'] == 2,
      (rl.meta(), dict(node('web-01'))))
for fmt in ('xlsx', 'json', 'yaml'):
    r = viewer.get(f'/api/rack/layout?format={fmt}')
    check(f'export {fmt} downloads', r.status_code == 200
          and 'attachment' in r.headers.get('Content-Disposition', ''))
    rows, errs, _ = rl.parse(f'export.{fmt}', r.data)
    check(f'export {fmt} round-trips through the importer', not errs and len(rows) == 4, errs)
    r = viewer.get(f'/api/rack/layout/template?format={fmt}')
    rows, errs, _ = rl.parse(f't.{fmt}', r.data)
    check(f'template {fmt} imports cleanly', r.status_code == 200 and not errs and len(rows) == 3,
          errs)
check('bad export format → 400', viewer.get('/api/rack/layout?format=pdf').status_code == 400)
check('layout JSON listing', len(viewer.get('/api/rack/layout').get_json()['rows']) == 4)
check('viewer cannot clear', viewer.delete('/api/rack/layout', headers=vh).status_code == 403)
r = admin.delete('/api/rack/layout', headers=ah)
check('clear → ok', r.status_code == 200 and rl.meta()['rows'] == 0)
na, nb = node('rack0_sled5_gpu'), node('web-01')
check('after clear, hostname topology is back', na['rack'] == 0 and na['sled'] == 5
      and na['topo_source'] == 'hostname', dict(na))
check('after clear, non-rack names are unracked again', nb['rack'] is None
      and nb['topo_source'] == '', dict(nb))

print('== discovery uses planned hostnames ==')
upload(admin, ah, 'plan.json', json.dumps({'nodes': [
    {'mac': 'aa:bb:cc:00:00:01', 'hostname': 'rack5-sled1-gpu', 'rack': 5, 'sled': 1, 'role': 'gpu'},
    {'mac': 'aa:bb:cc:00:00:02', 'rack': 5, 'sled': 2}]}).encode())
now = int(time.time()) + 3600
pathlib.Path(LEASES).write_text(
    f'{now} aa:bb:cc:00:00:01 192.168.100.61 ubuntu 01:aa\n'
    f'{now} aa:bb:cc:00:00:02 192.168.100.62 * 01:bb\n')
html = admin.get('/discovery').get_data(as_text=True)
check('discovery shows the planned name and position', 'rack5-sled1-gpu' in html
      and 'rack 5' in html)
r = admin.post('/api/discovery/import', headers=ah, json={'systems': [
    {'ip': '192.168.100.61', 'mac': 'AA:BB:CC:00:00:01', 'hostname': 'ubuntu'},
    {'ip': '192.168.100.62', 'mac': 'AA:BB:CC:00:00:02', 'hostname': ''}]})
names = [x['name'] for x in r.get_json()['results']]
check('import names the machine after the layout, not DHCP', names[0] == 'rack5-sled1-gpu', names)
check('layout row without hostname keeps the placeholder', names[1].startswith('node-'), names)
p = node(names[1])
check('placeholder-named node still placed by MAC', p['rack'] == 5 and p['sled'] == 2, dict(p))
r = admin.post('/api/discovery/import', headers=ah, json={'systems': [
    {'ip': '192.168.100.63', 'mac': 'AA:BB:CC:00:00:01', 'hostname': ''}]})
check('MAC already in inventory → exists', r.get_json()['results'][0]['status'] == 'exists')

print(f'\n{ok} passed, {fail} failed')
sys.exit(1 if fail else 0)
