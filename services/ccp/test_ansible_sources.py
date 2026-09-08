"""Ansible filesystem-source tests: source scanning (playbook heuristic,
roles, skip-dirs), path containment on resolve, the run API accepting
playbook_path, and the playbook-script deprecation.
"""
import os, sys, time, tempfile, pathlib

TMP = tempfile.mkdtemp(prefix='ccp_ansible_')
SRC = f'{TMP}/sources/site_a'
os.environ.update({
    'CCP_DB': f'{TMP}/ccp.db',
    'CCP_FILES_DIR': f'{TMP}/files',
    'CCP_JOBS_DIR': f'{TMP}/jobs',
    'CCP_SSH_KEY': f'{TMP}/ssh/id_ccp',
    'CCP_ANSIBLE_DIRS': f'{SRC}:{TMP}/sources/missing',
    'CCP_ADMIN_USER': 'admin',
    'CCP_ADMIN_PASSWORD': 'adminpass123',
    'CCP_TEST_SYNC_JOBS': '1',
})
pathlib.Path(f'{TMP}/ssh').mkdir(parents=True)
pathlib.Path(f'{TMP}/ssh/id_ccp').write_text('FAKE\n')
pathlib.Path(f'{TMP}/ssh/id_ccp.pub').write_text('ssh-ed25519 AAAATEST ccp\n')

# build a realistic source tree
root = pathlib.Path(SRC)
(root / 'roles' / 'common' / 'tasks').mkdir(parents=True)
(root / 'group_vars').mkdir()
(root / 'plays').mkdir()
(root / 'site.yml').write_text('---\n- name: site\n  hosts: all\n  tasks: []\n')
(root / 'plays' / 'gpu.yaml').write_text('---\n- hosts: gpu\n  tasks: []\n')
(root / 'group_vars' / 'all.yml').write_text('foo: bar\n')          # vars, not a playbook
(root / 'roles' / 'common' / 'tasks' / 'main.yml').write_text('- debug:\n')
(root / 'vars-only.yml').write_text('---\nsome_var: 1\n')           # no hosts:
(root / 'hosts').write_text('[all]\nlocalhost\n')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as ccp                                            # noqa: E402
import ansible_sources                                       # noqa: E402
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

print('== scanning ==')
srcs = ansible_sources.sources()
check('only existing dirs are sources (missing one created at startup counts)',
      SRC in ' '.join(srcs), srcs)
scan = ansible_sources.scan(SRC)
check('playbooks found (top-level + nested)',
      scan['playbooks'] == ['plays/gpu.yaml', 'site.yml'], scan['playbooks'])
check('vars files not mistaken for playbooks',
      'vars-only.yml' not in scan['playbooks'] and 'group_vars/all.yml' not in scan['playbooks'])
check('role internals not scanned as playbooks',
      not any('roles/' in p for p in scan['playbooks']))
check('roles listed', scan['roles'] == ['common'], scan['roles'])
check('inventory files listed', 'hosts' in scan['inventories'], scan['inventories'])

print('== resolve containment ==')
p = ansible_sources.resolve_playbook(SRC, 'site.yml')
check('valid resolve', p == str(root.resolve() / 'site.yml'), p)
for bad_src, bad_rel in ((SRC, '../outside.yml'), (SRC, '/etc/passwd'),
                         (SRC, 'nope.yml'), ('/etc', 'passwd'),
                         (SRC, 'roles/common/tasks/main.yml/../../../../../../etc/x.yml'),
                         (SRC, 'site.txt')):
    try:
        ansible_sources.resolve_playbook(bad_src, bad_rel)
        check(f'reject {bad_src}:{bad_rel}', False)
    except ValueError:
        check(f'reject {bad_src}:{bad_rel}', True)

print('== run API ==')
admin, ah = client_for('admin', 'adminpass123')
nid = db.execute("INSERT INTO nodes (name,address,conn,ssh_user,ssh_port,groups,created_at,state) "
                 "VALUES ('control','localhost','local','root',22,'',?,'managed')",
                 (int(time.time()),))
r = admin.post('/api/run/ansible', headers=ah,
               json={'playbook_path': 'site.yml', 'source': SRC, 'node_ids': [nid]})
check('run by playbook_path accepted', r.status_code == 201, r.get_json())
jid = r.get_json()['job_id']
job = db.query('SELECT * FROM jobs WHERE id=?', (jid,), one=True)
check('spec records the source path, no inline playbook',
      'site.yml' in job['spec'] and '"playbook"' not in job['spec'], job['spec'])
log = executor.job_log(jid)
check('ansible-playbook invoked with the source file', 'site.yml' in log, log)

r = admin.post('/api/run/ansible', headers=ah,
               json={'playbook_path': '../x.yml', 'source': SRC, 'node_ids': [nid]})
check('bad path rejected by API', r.status_code == 400)
r = admin.post('/api/run/ansible', headers=ah, json={'node_ids': [nid]})
check('neither path nor inline → 400', r.status_code == 400)

r = admin.get('/api/ansible/sources')
d = r.get_json()
check('sources API returns scan', any(s['source'] == str(root.resolve())
      and 'site.yml' in s['playbooks'] for s in d['sources']), d)

print('== playbook scripts deprecated ==')
r = admin.post('/api/scripts', headers=ah,
               json={'name': 'x', 'kind': 'playbook', 'content': '---'})
check('saving playbook-kind scripts rejected with pointer',
      r.status_code == 400 and 'deprecated' in r.get_json()['error'])
r = admin.post('/api/scripts', headers=ah,
               json={'name': 'sh1', 'kind': 'shell', 'content': 'uptime'})
check('shell scripts still save', r.status_code == 201)

print('== ansible page renders sources ==')
r = admin.get('/ansible')
check('page lists scanned playbooks', b'plays/gpu.yaml' in r.data and b'site.yml' in r.data)

print(f'\n{ok} passed, {fail} failed')
sys.exit(1 if fail else 0)
