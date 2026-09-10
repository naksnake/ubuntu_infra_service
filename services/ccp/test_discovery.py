"""DHCP discovery tests: dnsmasq lease parsing (v4/v6/malformed lines),
inventory cross-referencing, and the import/onboard API.

SSH is stubbed and jobs run inline (CCP_TEST_SYNC_JOBS=1).
"""
import os, sys, time, tempfile, pathlib

TMP = tempfile.mkdtemp(prefix='ccp_discovery_')
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
import app as ccp                                            # noqa: E402
import db                                                    # noqa: E402
import discovery                                             # noqa: E402
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

executor._password_ssh = lambda *a, **kw: (0, '')
executor._key_ssh = lambda a, u, p, cmd: (0, 'CCP_OK\nremote-host\n')

admin, ah = client_for('admin', 'adminpass123')
future = int(time.time()) + 3600
past = int(time.time()) - 3600

print('== lease parsing ==')
leases, err = discovery.parse_leases('/nonexistent/leases')
check('missing file → empty + error', leases == [] and err and 'not found' in err, err)

pathlib.Path(LEASES).write_text(
    f'{future} aa:bb:cc:dd:ee:01 192.168.100.21 gpu-node-1 01:aa:bb:cc:dd:ee:01\n'
    f'{future} aa:bb:cc:dd:ee:02 192.168.100.9 * 01:aa:bb:cc:dd:ee:02\n'
    f'{past} aa:bb:cc:dd:ee:03 192.168.100.77 stale-box *\n'
    f'0 aa:bb:cc:dd:ee:04 192.168.100.2 static-host *\n'
    f'{future} 12345678 fd00:100::10 v6-host 00:01:00:01:2a:aa:bb:cc\n'
    'garbage line\n'
    'notanumber aa:bb:cc:dd:ee:99 192.168.100.99 x *\n')
leases, err = discovery.parse_leases()
check('parses valid lines, skips malformed', err is None and len(leases) == 5,
      (err, len(leases)))
by_ip = {l['ip']: l for l in leases}
check('v4 lease fields', by_ip['192.168.100.21']['mac'] == 'AA:BB:CC:DD:EE:01'
      and by_ip['192.168.100.21']['hostname'] == 'gpu-node-1'
      and not by_ip['192.168.100.21']['expired'])
check("'*' hostname → empty", by_ip['192.168.100.9']['hostname'] == '')
check('expired lease flagged', by_ip['192.168.100.77']['expired'] is True)
check('infinite lease (0) not expired', by_ip['192.168.100.2']['expired'] is False)
check('DHCPv6 line: DUID shown as mac', by_ip['fd00:100::10']['mac'].startswith('00:01:00:01'))
check('sorted by IP', [l['ip'] for l in leases][:3]
      == ['192.168.100.2', '192.168.100.9', '192.168.100.21'])

print('== cross-referencing ==')
nid = db.execute("INSERT INTO nodes (name, address, conn, ssh_user, ssh_port, groups, "
                 "mac, state, created_at) VALUES ('known','192.168.100.50','ssh','root',22,'',"
                 "'AA:BB:CC:DD:EE:01','managed',?)", (int(time.time()),))
ann = discovery.annotate(discovery.parse_leases()[0])
by_ip = {l['ip']: l for l in ann}
check('lease matched by MAC even though IP differs',
      by_ip['192.168.100.21']['node_id'] == nid
      and by_ip['192.168.100.21']['node_state'] == 'managed')
check('unknown lease has no node', by_ip['192.168.100.9']['node_id'] is None)
check('new_system_count counts active unknown leases only',
      discovery.new_system_count() == 3, discovery.new_system_count())
# (100.9 unknown-active, 100.2 static-active, v6 active; 100.77 is expired)

print('== API ==')
r = admin.get('/api/discovery')
check('GET /api/discovery returns annotated leases',
      r.status_code == 200 and len(r.get_json()['leases']) == 5)

r = admin.post('/api/discovery/import', headers=ah, json={'systems': []})
check('empty import → 400', r.status_code == 400)
r = admin.post('/api/discovery/import', headers=ah,
               json={'systems': [{'ip': '192.168.100.9'}], 'username': 'u'})
check('username without password → 400', r.status_code == 400)

print('== import & onboard ==')
r = admin.post('/api/discovery/import', headers=ah, json={
    'systems': [{'ip': '192.168.100.9', 'mac': 'aa:bb:cc:dd:ee:02', 'hostname': ''},
                {'ip': '192.168.100.2', 'mac': 'aa:bb:cc:dd:ee:04', 'hostname': 'static-host'}],
    'username': 'ubuntu', 'password': 'pw'})
d = r.get_json()
check('import accepted', r.status_code == 201 and len(d['results']) == 2, d)
check('both systems onboarded', all(x['status'] == 'onboarding' and x['job_id']
                                    for x in d['results']), d)
n1 = db.query('SELECT * FROM nodes WHERE address=?', ('192.168.100.9',), one=True)
check('nameless system got placeholder then remote hostname',
      n1['name'] == 'remote-host', dict(n1))
check('MAC stored uppercase', n1['mac'] == 'AA:BB:CC:DD:EE:02')
check('node is managed after stubbed onboard', n1['state'] == 'managed')
n2 = db.query('SELECT * FROM nodes WHERE address=?', ('192.168.100.2',), one=True)
check('lease hostname kept as node name', n2['name'] == 'static-host')

print('== duplicates ==')
r = admin.post('/api/discovery/import', headers=ah, json={
    'systems': [{'ip': '192.168.100.9', 'mac': 'aa:bb:cc:dd:ee:02'},
                {'ip': '192.168.100.60', 'mac': 'AA:BB:CC:DD:EE:01'}]})
d = r.get_json()
check('re-import by MAC/IP reports exists, no duplicate rows',
      all(x['status'] == 'exists' for x in d['results']), d)
check('node count unchanged',
      db.query('SELECT COUNT(*) AS c FROM nodes')[0]['c'] == 3)

print('== import only (no credentials) ==')
r = admin.post('/api/discovery/import', headers=ah, json={
    'systems': [{'ip': '192.168.100.77', 'mac': 'aa:bb:cc:dd:ee:03', 'hostname': 'stale-box'}]})
d = r.get_json()
check('import-only creates discovered node',
      d['results'][0]['status'] == 'imported'
      and db.query("SELECT state FROM nodes WHERE address='192.168.100.77'",
                   one=True)['state'] == 'discovered', d)
r = admin.post('/api/run/shell', headers=ah, json={
    'command': 'echo x',
    'node_ids': [d['results'][0]['node_id']]})
check('discovered node cannot run jobs yet', r.status_code == 400)

print('== RBAC ==')
admin.post('/api/users', headers=ah,
           json={'username': 'v1', 'password': 'password123', 'role': 'viewer'})
viewer, vh = client_for('v1', 'password123')
r = viewer.post('/api/discovery/import', headers=vh,
                json={'systems': [{'ip': '10.0.0.1'}]})
check('viewer cannot import', r.status_code == 403)
r = viewer.get('/api/discovery')
check('viewer can view discovery', r.status_code == 200)

print(f'\n{ok} passed, {fail} failed')
sys.exit(1 if fail else 0)
