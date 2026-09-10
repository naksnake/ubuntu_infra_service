"""Cluster Control Panel (CCP) — a lightweight, self-hosted control panel for a
PXE lab: run shell commands across nodes with ClusterShell, run Ansible
playbooks, keep a script repository, browse job history and audit logs, and
share files — all behind session login with role-based access control.

Roles (increasing privilege): viewer < operator < admin
  viewer   — read-only on cluster ops: nodes, jobs + output, scripts
  operator — viewer + run jobs, manage nodes/scripts
  admin    — operator + manage users, view audit log, delete jobs

Files: every authenticated user gets a private per-user file space under
CCP_FILES_DIR/<username>/ (auto-created on first use, subfolders supported).
A user can only ever see or touch paths inside their own root — the root is
derived from the server-side session, never from request data, and every
client-supplied path is validated segment-by-segment and containment-checked
after resolution. To restrict uploads to operators again, re-add
@require('operator') on the file mutation routes below.
"""
import os
import re
import json
import time
import functools
import secrets
import pathlib

from flask import (Flask, request, session, redirect, url_for, render_template,
                   jsonify, Response, abort, send_file, flash)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

import ansible_sources
import db
import discovery
import executor
import topology

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get('CCP_SECRET_KEY') or secrets.token_hex(32),
    MAX_CONTENT_LENGTH=int(os.environ.get('CCP_MAX_UPLOAD_MB', '2048')) * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    PERMANENT_SESSION_LIFETIME=int(os.environ.get('CCP_SESSION_HOURS', '12')) * 3600,
    # Browser cookies are scoped by hostname only — NOT by port — so every web
    # UI on this host needs its own cookie name, or logging in to one UI
    # overwrites (and kills) the session of the others.
    SESSION_COOKIE_NAME='lab_ccp_session',
)

FILES_DIR = pathlib.Path(os.environ.get('CCP_FILES_DIR', '/data/ccp/files'))
# Default per-user quota in MB; 0 disables the quota. users.quota_mb (nullable)
# overrides this per user.
USER_QUOTA_MB = int(os.environ.get('CCP_USER_QUOTA_MB', '10240'))

# A username doubles as an on-disk directory name, so it must be a single safe
# path segment: ASCII, starts with alphanumeric (no dot-prefixed / hidden
# names), and contains no separators. Enforced both at user creation and again
# every time a storage path is derived (defense in depth).
USERNAME_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,31}')
# Same shape for every folder / file path segment a client may supply.
SEGMENT_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}')
MAX_TREE_DEPTH = 8          # max folder nesting below the user root
MAX_RELPATH_LEN = 512       # max total length of a client-supplied rel path

ROLES = ('viewer', 'operator', 'admin')
_RANK = {r: i for i, r in enumerate(ROLES)}

db.init_db()
executor.ensure_ssh_key()   # CCP's ed25519 identity, installed by onboarding
ansible_sources.ensure_default_dirs()

# node lifecycle states (see docs/ccp/RFC-0001-lifecycle-platform.md §4)
NODE_STATES = ('discovered', 'onboarding', 'managed', 'failed', 'unverified')


@app.template_filter('tstime')
def _tstime(ts):
    if not ts:
        return '—'
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(int(ts)))


# ── auth / rbac ───────────────────────────────────────────────────────────────

PUBLIC_PATHS = {'/login', '/healthz'}


@app.before_request
def _guard():
    if request.path in PUBLIC_PATHS or request.path.startswith('/static/'):
        return None
    if not session.get('uid'):
        if request.path.startswith('/api/'):
            return jsonify({'error': 'authentication required'}), 401
        return redirect(url_for('login', next=request.path))
    # CSRF for state-changing requests
    if request.method in ('POST', 'PUT', 'DELETE', 'PATCH'):
        token = request.headers.get('X-CSRF-Token') or request.form.get('_csrf')
        if not token or token != session.get('csrf'):
            return jsonify({'error': 'invalid or missing CSRF token'}), 403
    return None


def current_user():
    return {'id': session.get('uid'), 'username': session.get('username'),
            'role': session.get('role')}


def role_ok(minimum):
    return _RANK.get(session.get('role'), -1) >= _RANK[minimum]


def require(minimum):
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            if not role_ok(minimum):
                if request.path.startswith('/api/'):
                    return jsonify({'error': 'forbidden: requires %s' % minimum}), 403
                abort(403)
            return fn(*a, **kw)
        return wrapper
    return deco


def _client_ip():
    return (request.headers.get('X-Forwarded-For', '').split(',')[0].strip()
            or request.remote_addr or '')


def log_action(action, detail=''):
    db.audit(session.get('username'), action, detail, _client_ip())


@app.context_processor
def _inject():
    return {'user': current_user(), 'csrf_token': session.get('csrf'),
            'role_ok': role_ok}


@app.after_request
def _security_headers(resp):
    resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
    resp.headers.setdefault('X-Frame-Options', 'DENY')
    resp.headers.setdefault('Referrer-Policy', 'no-referrer')
    return resp


# ── brute-force lockout ───────────────────────────────────────────────────────
# After LOGIN_FAIL_LIMIT failed logins from one IP within LOGIN_FAIL_WINDOW
# seconds, further attempts from that IP get 429 until the window rolls over.
# Counters are per worker process (effective budget = workers x limit).
LOGIN_FAIL_LIMIT  = int(os.environ.get('LOGIN_FAIL_LIMIT', '5'))
LOGIN_FAIL_WINDOW = int(os.environ.get('LOGIN_FAIL_WINDOW', '900'))
_login_fails = {}


def _locked_out(ip):
    now = time.time()
    hits = [t for t in _login_fails.get(ip, []) if now - t < LOGIN_FAIL_WINDOW]
    if hits:
        _login_fails[ip] = hits
    else:
        _login_fails.pop(ip, None)
    return len(hits) >= LOGIN_FAIL_LIMIT


@app.route('/healthz')
def healthz():
    return Response('ok', mimetype='text/plain')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        # lockout keys on the real peer address — X-Forwarded-For is client-
        # controlled here (no reverse proxy) and would be trivial to rotate
        ip = request.remote_addr or ''
        if _locked_out(ip):
            db.audit('-', 'login', 'locked out (too many failures)', ip)
            flash(f'Too many failed attempts — this address is locked for '
                  f'{LOGIN_FAIL_WINDOW // 60} minutes.')
            return render_template('login.html'), 429
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        row = db.query('SELECT * FROM users WHERE username=?', (username,), one=True)
        if row and check_password_hash(row['password_hash'], password):
            _login_fails.pop(ip, None)
            session.clear()
            session['uid'] = row['id']
            session['username'] = row['username']
            session['role'] = row['role']
            session['csrf'] = secrets.token_hex(16)
            session.permanent = True
            db.audit(username, 'login', 'success', _client_ip())
            nxt = request.args.get('next', '')
            return redirect(nxt if nxt.startswith('/') else url_for('dashboard'))
        _login_fails.setdefault(ip, []).append(time.time())
        db.audit(username or '-', 'login', 'failed', ip)
        flash('Invalid username or password.')
    return render_template('login.html')


@app.route('/logout', methods=['POST'])
def logout():
    log_action('logout')
    session.clear()
    return redirect(url_for('login'))


# ── pages ─────────────────────────────────────────────────────────────────────

@app.route('/')
def dashboard():
    lifecycle = {s: 0 for s in NODE_STATES}
    for r in db.query('SELECT state, COUNT(*) AS c FROM nodes GROUP BY state'):
        if r['state'] in lifecycle:
            lifecycle[r['state']] = r['c']
    jobs = db.query('SELECT COUNT(*) AS c FROM jobs')[0]['c']
    running = db.query("SELECT COUNT(*) AS c FROM jobs WHERE status='running'")[0]['c']
    recent = db.query('SELECT * FROM jobs ORDER BY id DESC LIMIT 8')
    return render_template('dashboard.html', lifecycle=lifecycle,
                           new_systems=discovery.new_system_count(),
                           stats={'jobs': jobs, 'running': running},
                           recent=recent)


@app.route('/nodes')
def nodes_page():
    nodes = db.query(
        'SELECT n.*, h.cpu_cores, h.mem_mb, h.gpu_count, h.gpu_model, h.os_name '
        'FROM nodes n LEFT JOIN hardware h ON h.node_id = n.id ORDER BY n.name')
    return render_template('nodes.html', nodes=nodes)


@app.route('/deploy')
def deploy_page():
    """Staging assets → target selection → deployment, with live per-node
    results. Files come from the caller's own file space."""
    root = _user_root()
    staged = []
    for base, _dirs, names in os.walk(root):
        for n in sorted(names):
            if n.startswith('.'):
                continue
            fp = pathlib.Path(base) / n
            if not fp.is_file() or fp.is_symlink():
                continue
            try:
                st = fp.stat()
            except OSError:
                continue
            staged.append({'path': str(fp.relative_to(root)),
                           'size': st.st_size, 'mtime': int(st.st_mtime)})
    staged.sort(key=lambda f: f['path'])
    return render_template('deploy.html', staged=staged)


@app.route('/rack')
def rack_page():
    """Rack view generated purely from hostname topology — nothing is drawn
    or configured by hand."""
    nodes = db.query(
        'SELECT n.*, h.gpu_count, h.gpu_model, h.cpu_cores, h.mem_mb '
        'FROM nodes n LEFT JOIN hardware h ON h.node_id = n.id ORDER BY n.name')
    racks = {}
    unracked = []
    for n in nodes:
        if n['rack'] is None:
            unracked.append(n)
        else:
            racks.setdefault(n['rack'], []).append(n)
    racks = {r: sorted(v, key=lambda n: (n['sled'] or 0))
             for r, v in sorted(racks.items())}
    return render_template('rack.html', racks=racks, unracked=unracked)


@app.route('/discovery')
def discovery_page():
    leases, err = discovery.parse_leases()
    return render_template('discovery.html',
                           leases=discovery.annotate(leases), error=err)


@app.route('/shell')
def shell_page():
    return render_template('shell.html',
                           nodes=db.query('SELECT * FROM nodes ORDER BY name'),
                           scripts=db.query("SELECT * FROM scripts WHERE kind='shell' ORDER BY name"))


@app.route('/ansible')
def ansible_page():
    return render_template('ansible.html',
                           nodes=db.query('SELECT * FROM nodes ORDER BY name'),
                           sources=ansible_sources.scan_all(),
                           configured_dirs=ansible_sources.ANSIBLE_DIRS,
                           scripts=db.query("SELECT * FROM scripts WHERE kind='playbook' ORDER BY name"))


@app.route('/jobs')
def jobs_page():
    return render_template('jobs.html',
                           jobs=db.query('SELECT * FROM jobs ORDER BY id DESC LIMIT 200'),
                           stats=executor.jobs_stats(),
                           kinds=[r['kind'] for r in
                                  db.query('SELECT DISTINCT kind FROM jobs ORDER BY kind')])


@app.route('/jobs/<int:job_id>')
def job_detail(job_id):
    job = db.query('SELECT * FROM jobs WHERE id=?', (job_id,), one=True)
    if not job:
        abort(404)
    return render_template('job_detail.html', job=job)


@app.route('/scripts')
def scripts_page():
    rows = db.query('SELECT * FROM scripts ORDER BY name')
    # embed full content for the editor; escape '<' so a "</script>" inside a
    # script body can't break out of the JSON <script> block
    payload = json.dumps([dict(r) for r in rows]).replace('<', '\\u003c')
    return render_template('scripts.html', scripts=rows, scripts_json=payload)


@app.route('/files')
def files_page():
    rel = _safe_rel(request.args.get('folder', ''))
    root = _user_root()
    cur = _inside(root, rel)
    if not cur.is_dir():
        abort(404)
    folders, files = [], []
    for e in sorted(cur.iterdir(), key=lambda p: p.name.lower()):
        if e.name.startswith('.') or e.is_symlink():
            continue                      # hide temp files; never follow links
        if e.is_dir():
            folders.append({'name': e.name,
                            'path': str(rel / e.name)})
        elif e.is_file():
            st = e.stat()
            files.append({'name': e.name, 'path': str(rel / e.name),
                          'size': st.st_size, 'mtime': int(st.st_mtime)})
    # breadcrumb: [('Home', ''), ('docs', 'docs'), ('2026', 'docs/2026')]
    crumbs, acc = [('Home', '')], []
    for part in rel.parts:
        acc.append(part)
        crumbs.append((part, '/'.join(acc)))
    return render_template('files.html', folders=folders, files=files,
                           rel=str(rel) if rel.parts else '', crumbs=crumbs,
                           quota_mb=_user_quota_bytes() // (1024 * 1024),
                           used_mb=_tree_size(root) // (1024 * 1024))


@app.route('/users')
@require('admin')
def users_page():
    return render_template('users.html',
                           users=db.query('SELECT id, username, role, created_at FROM users ORDER BY username'),
                           roles=ROLES)


@app.route('/audit')
@require('admin')
def audit_page():
    return render_template('audit.html',
                           rows=db.query('SELECT * FROM audit ORDER BY id DESC LIMIT 500'))


# ── nodes API ───────────────────────────────────────────────────────────────

NODE_NAME_RE = re.compile(r'[A-Za-z0-9._-]{1,63}')
NODE_ADDR_RE = re.compile(r'[A-Za-z0-9._:-]{1,255}')
NODE_USER_RE = re.compile(r'[A-Za-z0-9._-]{1,32}')


def _validate_node_fields(d, name_required):
    """Shared validation for node create/onboard. Returns (fields, error).

    names/addresses/users flow into a ClusterShell NodeSet and an Ansible
    inventory file, so restrict them to safe characters (no whitespace,
    newlines, '=', or NodeSet range brackets that would corrupt either)."""
    name = (d.get('name') or '').strip()
    address = (d.get('address') or '').strip()
    ssh_user = (d.get('username') or d.get('ssh_user') or 'root').strip()
    if not address:
        return None, 'address is required'
    if name_required and not name:
        return None, 'name is required'
    if name and not NODE_NAME_RE.fullmatch(name):
        return None, 'name may contain only letters, digits, dot, dash, underscore'
    if not NODE_ADDR_RE.fullmatch(address):
        return None, 'address may contain only letters, digits, dot, colon, dash'
    if not NODE_USER_RE.fullmatch(ssh_user):
        return None, 'ssh user may contain only letters, digits, dot, dash, underscore'
    try:
        port = int(d.get('ssh_port') or 22)
    except (TypeError, ValueError):
        return None, 'ssh port must be a number'
    if not (1 <= port <= 65535):
        return None, 'ssh port must be 1-65535'
    mac = (d.get('mac') or '').strip().upper()
    if mac and not re.fullmatch(r'[0-9A-F:.-]{1,23}', mac):
        return None, 'mac address contains invalid characters'
    return {'name': name, 'address': address, 'ssh_user': ssh_user,
            'ssh_port': port, 'mac': mac,
            'groups': (d.get('groups') or '').strip()}, None


def _placeholder_name(address):
    """Unique placeholder for a node imported without a name; the onboarding
    job replaces it with the real remote hostname."""
    base = 'node-' + re.sub(r'[^A-Za-z0-9]+', '-', address).strip('-')[:50]
    name, i = base, 1
    while db.query('SELECT 1 FROM nodes WHERE name=?', (name,), one=True):
        i += 1
        name = f'{base}-{i}'
    return name


def _start_onboarding(node, password, set_hostname=True):
    """Queue the onboarding job for an ssh node and mark it in progress."""
    db.execute("UPDATE nodes SET state='onboarding', state_detail='' WHERE id=?",
               (node['id'],))
    return executor.start_job(
        'onboard', node['name'],
        {'node_id': node['id'], 'mode': 'onboard',
         'auto_name': bool(node.get('auto_name')),
         'set_hostname': bool(set_hostname)},
        session['username'], secret=password)


@app.route('/api/nodes', methods=['POST'])
@require('operator')
def api_add_node():
    """Onboard a node. For conn='ssh' this creates the row in state
    'onboarding' and queues the lifecycle job (credential validation → key
    bootstrap → execution check); the node becomes 'managed' only if all three
    pass. conn='local' (this host) needs no credentials and is managed
    immediately. The password lives in memory for the job only."""
    d = request.get_json(force=True) or {}
    conn = 'local' if d.get('conn') == 'local' else 'ssh'
    fields, err = _validate_node_fields(d, name_required=(conn == 'local'))
    if err:
        return jsonify({'error': err}), 400
    password = d.get('password') or ''
    if conn == 'ssh':
        if not (d.get('username') or '').strip() or not password:
            return jsonify({'error': 'username and password are required to '
                            'onboard an SSH node'}), 400
        if len(password) > 256:
            return jsonify({'error': 'password too long'}), 400
    auto_name = not fields['name']
    # default on: the node's own hostname should match its inventory name, so
    # the rack view and the machine describe the same box
    set_hostname = d.get('set_hostname', True) is not False
    name = fields['name'] or _placeholder_name(fields['address'])
    state = 'managed' if conn == 'local' else 'onboarding'
    try:
        node_id = db.execute(
            'INSERT INTO nodes (name, address, conn, ssh_user, ssh_port, groups, '
            'mac, state, created_at) VALUES (?,?,?,?,?,?,?,?,?)',
            (name, fields['address'], conn, fields['ssh_user'], fields['ssh_port'],
             fields['groups'], fields['mac'], state, int(time.time())))
    except Exception as exc:
        return jsonify({'error': f'could not add node: {exc}'}), 400
    topology.apply(node_id, name)
    log_action('node.add', f'{name} ({fields["address"]}, {conn})')
    if conn == 'local':
        return jsonify({'id': node_id}), 201
    job_id = executor.start_job(
        'onboard', name,
        {'node_id': node_id, 'mode': 'onboard', 'auto_name': auto_name,
         'set_hostname': set_hostname},
        session['username'], secret=password)
    log_action('node.onboard', f'{name} job {job_id}')
    return jsonify({'id': node_id, 'job_id': job_id}), 201


@app.route('/api/nodes/<int:node_id>/onboard', methods=['POST'])
@require('operator')
def api_onboard_node(node_id):
    """(Re)run onboarding for an existing ssh node (discovered / failed /
    unverified — or managed, e.g. to rotate the SSH user)."""
    node = db.query('SELECT * FROM nodes WHERE id=?', (node_id,), one=True)
    if not node:
        return jsonify({'error': 'not found'}), 404
    if node['conn'] == 'local':
        return jsonify({'error': 'local nodes do not need onboarding'}), 400
    if node['state'] == 'onboarding':
        return jsonify({'error': 'onboarding is already in progress'}), 409
    d = request.get_json(force=True) or {}
    username = (d.get('username') or '').strip()
    password = d.get('password') or ''
    if not username or not password:
        return jsonify({'error': 'username and password are required'}), 400
    if len(password) > 256:
        return jsonify({'error': 'password too long'}), 400
    if not NODE_USER_RE.fullmatch(username):
        return jsonify({'error': 'ssh user may contain only letters, digits, '
                        'dot, dash, underscore'}), 400
    db.execute('UPDATE nodes SET ssh_user=? WHERE id=?', (username, node_id))
    node = dict(node)
    node['auto_name'] = node['name'].startswith('node-')
    job_id = _start_onboarding(node, password,
                               set_hostname=d.get('set_hostname', True) is not False)
    log_action('node.onboard', f'{node["name"]} job {job_id}')
    return jsonify({'job_id': job_id}), 202


@app.route('/api/nodes/<int:node_id>/verify', methods=['POST'])
@require('operator')
def api_verify_node(node_id):
    """Key-only execution check: promotes a legacy 'unverified' node (whose
    key access already works) to 'managed' without needing a password."""
    node = db.query('SELECT * FROM nodes WHERE id=?', (node_id,), one=True)
    if not node:
        return jsonify({'error': 'not found'}), 404
    if node['conn'] == 'local':
        return jsonify({'error': 'local nodes do not need verification'}), 400
    if node['state'] == 'onboarding':
        return jsonify({'error': 'onboarding is already in progress'}), 409
    db.execute("UPDATE nodes SET state='onboarding', "
               "state_detail='verifying key access' WHERE id=?", (node_id,))
    job_id = executor.start_job('verify', node['name'],
                                {'node_id': node_id, 'mode': 'verify'},
                                session['username'])
    log_action('node.verify', f'{node["name"]} job {job_id}')
    return jsonify({'job_id': job_id}), 202


@app.route('/api/nodes/<int:node_id>/hostname', methods=['POST'])
@require('operator')
def api_set_hostname(node_id):
    """One-click hostname change: hostnamectl set-hostname (+ /etc/hostname +
    /etc/hosts) on the node, then the inventory name and derived rack/sled/role
    refresh immediately when the job confirms."""
    node = db.query('SELECT * FROM nodes WHERE id=?', (node_id,), one=True)
    if not node:
        return jsonify({'error': 'not found'}), 404
    if node['conn'] == 'local':
        return jsonify({'error': 'rename the CCP host from its own shell, '
                        'not the panel'}), 400
    if not executor.node_eligible(node):
        return jsonify({'error': 'only managed nodes can be renamed — '
                        'onboard the node first'}), 400
    d = request.get_json(force=True) or {}
    new_name = (d.get('hostname') or '').strip()
    if not NODE_NAME_RE.fullmatch(new_name):
        return jsonify({'error': 'hostname may contain only letters, digits, '
                        'dot, dash, underscore (max 63 chars)'}), 400
    if new_name == node['name']:
        return jsonify({'error': 'that is already the node\'s name'}), 400
    if db.query('SELECT 1 FROM nodes WHERE name=?', (new_name,), one=True):
        return jsonify({'error': 'another node already uses that name'}), 409
    job_id = executor.start_job('hostname', node['name'],
                                {'node_id': node_id, 'new_name': new_name},
                                session['username'])
    log_action('node.hostname', f'{node["name"]} → {new_name} (job {job_id})')
    return jsonify({'job_id': job_id}), 202


@app.route('/api/nodes/<int:node_id>/hwscan', methods=['POST'])
@require('operator')
def api_hwscan_node(node_id):
    node = db.query('SELECT * FROM nodes WHERE id=?', (node_id,), one=True)
    if not node:
        return jsonify({'error': 'not found'}), 404
    if not executor.node_eligible(node):
        return jsonify({'error': 'only managed nodes can be scanned — '
                        'onboard the node first'}), 400
    job_id = executor.start_job('hwscan', node['name'], {'node_id': node_id},
                                session['username'])
    log_action('node.hwscan', f'{node["name"]} job {job_id}')
    return jsonify({'job_id': job_id}), 202


@app.route('/api/nodes')
def api_list_nodes():
    rows = db.query('SELECT * FROM nodes ORDER BY name')
    return jsonify({'nodes': [dict(r) for r in rows]})


@app.route('/api/nodes/<int:node_id>')
def api_node_detail(node_id):
    node = db.query('SELECT * FROM nodes WHERE id=?', (node_id,), one=True)
    if not node:
        return jsonify({'error': 'not found'}), 404
    hw = db.query('SELECT * FROM hardware WHERE node_id=?', (node_id,), one=True)
    return jsonify({'node': dict(node), 'hardware': dict(hw) if hw else None})


@app.route('/api/nodes/<int:node_id>', methods=['DELETE'])
@require('operator')
def api_delete_node(node_id):
    db.execute('DELETE FROM nodes WHERE id=?', (node_id,))
    log_action('node.delete', str(node_id))
    return jsonify({'ok': True})


# ── discovery API ─────────────────────────────────────────────────────────────

@app.route('/api/discovery')
def api_discovery():
    leases, err = discovery.parse_leases()
    return jsonify({'leases': discovery.annotate(leases), 'error': err})


@app.route('/api/discovery/import', methods=['POST'])
@require('operator')
def api_discovery_import():
    """Import discovered systems into the inventory. Each system: {ip, mac,
    hostname?}. With username+password each new node is onboarded immediately
    (state walks onboarding → managed/failed); without credentials it is
    imported as 'discovered' for later onboarding. Systems already matching a
    node by MAC or address are reported, not duplicated."""
    d = request.get_json(force=True) or {}
    systems = d.get('systems') or []
    username = (d.get('username') or '').strip()
    password = d.get('password') or ''
    set_hostname = d.get('set_hostname', True) is not False
    if not isinstance(systems, list) or not systems:
        return jsonify({'error': 'select at least one discovered system'}), 400
    if len(systems) > 500:
        return jsonify({'error': 'too many systems in one import'}), 400
    if (username and not password) or (password and not username):
        return jsonify({'error': 'supply both username and password to onboard, '
                        'or neither to import only'}), 400
    if username and not NODE_USER_RE.fullmatch(username):
        return jsonify({'error': 'ssh user may contain only letters, digits, '
                        'dot, dash, underscore'}), 400
    if len(password) > 256:
        return jsonify({'error': 'password too long'}), 400

    results, seen = [], set()
    for s in systems:
        ip = (s.get('ip') or '').strip()
        mac = (s.get('mac') or '').strip().upper()
        hostname = (s.get('hostname') or '').strip()
        if not ip or not NODE_ADDR_RE.fullmatch(ip):
            results.append({'ip': ip, 'status': 'error', 'error': 'invalid address'})
            continue
        if mac and not re.fullmatch(r'[0-9A-F:.-]{1,23}', mac):
            mac = ''
        key = mac or ip
        if key in seen:
            continue
        seen.add(key)
        existing = None
        if mac:
            existing = db.query('SELECT id, name FROM nodes WHERE upper(mac)=?',
                                (mac,), one=True)
        existing = existing or db.query('SELECT id, name FROM nodes WHERE address=?',
                                        (ip,), one=True)
        if existing:
            results.append({'ip': ip, 'node_id': existing['id'],
                            'status': 'exists', 'name': existing['name']})
            continue
        # dnsmasq hostnames are client-supplied; use only if safe and unique
        auto_name = True
        name = ''
        if hostname and NODE_NAME_RE.fullmatch(hostname) and not db.query(
                'SELECT 1 FROM nodes WHERE name=?', (hostname,), one=True):
            name, auto_name = hostname, False
        name = name or _placeholder_name(ip)
        state = 'onboarding' if username else 'discovered'
        node_id = db.execute(
            'INSERT INTO nodes (name, address, conn, ssh_user, ssh_port, groups, '
            'mac, state, created_at) VALUES (?,?,?,?,?,?,?,?,?)',
            (name, ip, 'ssh', username or 'root', 22, '', mac, state,
             int(time.time())))
        topology.apply(node_id, name)
        log_action('node.import', f'{name} ({ip}, {mac or "no mac"})')
        job_id = None
        if username:
            job_id = executor.start_job(
                'onboard', name,
                {'node_id': node_id, 'mode': 'onboard', 'auto_name': auto_name,
                 'set_hostname': set_hostname},
                session['username'], secret=password)
            log_action('node.onboard', f'{name} job {job_id}')
        results.append({'ip': ip, 'node_id': node_id, 'job_id': job_id,
                        'status': 'onboarding' if username else 'imported',
                        'name': name})
    return jsonify({'results': results}), 201


# ── file deployment API ───────────────────────────────────────────────────────

@app.route('/api/deploy/targets')
def api_deploy_targets():
    """Groups with their member nodes, for the two-level target selector.
    Only managed (or local) nodes can receive a deployment."""
    rows = db.query('SELECT * FROM nodes ORDER BY name')
    groups = {}
    for r in rows:
        node = {'id': r['id'], 'name': r['name'], 'address': r['address'],
                'state': r['state'], 'conn': r['conn'],
                'eligible': bool(executor.node_eligible(r))}
        names = [g.strip() for g in (r['groups'] or '').split(',') if g.strip()]
        for g in (names or ['ungrouped']):
            groups.setdefault(g, []).append(node)
    return jsonify({'groups': [{'name': g, 'nodes': n}
                               for g, n in sorted(groups.items())]})


@app.route('/api/deploy/files', methods=['POST'])
@require('operator')
def api_deploy_files():
    """Push staged files from the caller's own file space to selected
    nodes/groups. Body: {files:[relpath], node_ids:[], groups:[], dest, method}.

    Sources are resolved inside the caller's storage root with the same layered
    validation as the files API, so a deploy can never read another user's
    space or escape the root."""
    d = request.get_json(force=True) or {}
    rel_files = d.get('files') or []
    dest = (d.get('dest') or '').strip()
    method = 'ansible' if d.get('method') == 'ansible' else 'clush'
    if not rel_files:
        return jsonify({'error': 'select at least one staged file'}), 400
    if len(rel_files) > 64:
        return jsonify({'error': 'too many files in one deployment'}), 400
    # The destination reaches clush/ansible as an argv element, never a shell
    # string — but keep it to a strict allowlist anyway (same stance as every
    # other path this panel accepts) so no metacharacter can ever matter.
    if not re.fullmatch(r'/[A-Za-z0-9._/-]{0,511}', dest) or '..' in dest:
        return jsonify({'error': 'destination must be an absolute path on the '
                        'target nodes using letters, digits, dot, dash, '
                        'underscore and / — e.g. /opt/assets'}), 400

    root = _user_root()
    srcs = []
    for rel in rel_files:
        p = _inside(root, _safe_rel(rel))
        if not p.is_file():
            return jsonify({'error': f'not a staged file: {rel}'}), 404
        srcs.append(str(p))

    # group names expand to their member nodes, then the lifecycle gate applies
    ids = [int(x) for x in (d.get('node_ids') or []) if str(x).isdigit()]
    wanted_groups = [str(g).strip() for g in (d.get('groups') or []) if str(g).strip()]
    if wanted_groups:
        for r in db.query('SELECT id, groups FROM nodes'):
            names = [g.strip() for g in (r['groups'] or '').split(',') if g.strip()]
            if not names:
                names = ['ungrouped']
            if any(g in wanted_groups for g in names):
                ids.append(r['id'])
    ids, names, excluded = _selected_nodes({'node_ids': sorted(set(ids))})
    if not ids:
        return jsonify({'error': _target_error(excluded)}), 400

    job_id = executor.start_job(
        'filedeploy', ','.join(names),
        {'node_ids': ids, 'srcs': srcs, 'dest': dest, 'method': method},
        session['username'])
    log_action('deploy.files',
               f'{len(srcs)} file(s) → {dest} on {len(ids)} node(s) via {method} '
               f'(job {job_id})')
    return jsonify({'job_id': job_id, 'nodes': names,
                    'excluded': excluded}), 202


# ── job launch API ────────────────────────────────────────────────────────────

def _selected_nodes(d):
    """Expand node_ids + group into eligible targets, enforcing
    the lifecycle gate: only managed ssh nodes (or local nodes) run jobs.
    Returns (ids, names, excluded) where excluded lists 'name (state)' strings
    for selected-but-ineligible nodes so the error can name them."""
    ids = [int(x) for x in d.get('node_ids', []) if str(x).isdigit()]
    group = (d.get('group') or '').strip()
    if group:
        for r in db.query('SELECT id, groups FROM nodes'):
            if group in [g.strip() for g in (r['groups'] or '').split(',') if g.strip()]:
                ids.append(r['id'])
    ids = sorted(set(ids))
    if not ids:
        return [], [], []
    rows = db.query('SELECT * FROM nodes WHERE id IN (%s)'
                    % ','.join('?' for _ in ids), tuple(ids))
    eligible = [r for r in rows if executor.node_eligible(r)]
    excluded = [f"{r['name']} ({r['state']})" for r in rows
                if not executor.node_eligible(r)]
    return ([r['id'] for r in eligible], [r['name'] for r in eligible], excluded)


def _target_error(excluded):
    if excluded:
        return ('these nodes are not managed yet and cannot run jobs: '
                + ', '.join(excluded) + ' — onboard or verify them first')
    return 'select at least one node or a group'


@app.route('/api/run/shell', methods=['POST'])
@require('operator')
def api_run_shell():
    d = request.get_json(force=True) or {}
    command = (d.get('command') or '').strip()
    if not command:
        return jsonify({'error': 'command is required'}), 400
    ids, names, excluded = _selected_nodes(d)
    if not ids:
        return jsonify({'error': _target_error(excluded)}), 400
    job_id = executor.start_job('shell', ','.join(names),
                                {'node_ids': ids, 'command': command},
                                session['username'])
    log_action('run.shell', f'job {job_id}: {command[:120]}')
    return jsonify({'job_id': job_id}), 201


@app.route('/api/run/ansible', methods=['POST'])
@require('operator')
def api_run_ansible():
    """Run a playbook: either from a scanned filesystem source
    ({source, playbook_path}) or inline YAML ({playbook}, ad-hoc)."""
    d = request.get_json(force=True) or {}
    playbook = d.get('playbook') or ''
    playbook_path = ''
    label = ''
    if d.get('playbook_path'):
        try:
            playbook_path = ansible_sources.resolve_playbook(
                d.get('source') or '', d.get('playbook_path') or '')
        except ValueError as exc:
            return jsonify({'error': str(exc)}), 400
        label = d.get('playbook_path')
    elif not playbook.strip():
        return jsonify({'error': 'playbook content is required'}), 400
    ids, names, excluded = _selected_nodes(d)
    if not ids:
        return jsonify({'error': _target_error(excluded)}), 400
    spec = {'node_ids': ids, 'extra_vars': (d.get('extra_vars') or '').strip()}
    if playbook_path:
        spec['playbook_path'] = playbook_path
    else:
        spec['playbook'] = playbook
    job_id = executor.start_job('ansible', ','.join(names), spec,
                                session['username'])
    log_action('run.ansible', f'job {job_id}' + (f' ({label})' if label else ''))
    return jsonify({'job_id': job_id}), 201


@app.route('/api/ansible/sources')
def api_ansible_sources():
    return jsonify({'sources': ansible_sources.scan_all(),
                    'configured': ansible_sources.ANSIBLE_DIRS})


@app.route('/api/jobs/<int:job_id>')
def api_job(job_id):
    job = db.query('SELECT * FROM jobs WHERE id=?', (job_id,), one=True)
    if not job:
        return jsonify({'error': 'not found'}), 404
    return jsonify({'id': job['id'], 'kind': job['kind'], 'target': job['target'],
                    'status': job['status'], 'exit_code': job['exit_code'],
                    'created_by': job['created_by'], 'created_at': job['created_at'],
                    'finished_at': job['finished_at'],
                    'output': executor.job_log(job_id)})


@app.route('/api/jobs/<int:job_id>/log')
def api_job_log(job_id):
    """The raw job log as a downloadable text file — the thing to attach when
    asking for help with a failed deploy."""
    job = db.query('SELECT * FROM jobs WHERE id=?', (job_id,), one=True)
    if not job:
        return jsonify({'error': 'not found'}), 404
    resp = app.response_class(executor.job_log(job_id), mimetype='text/plain')
    resp.headers['Content-Disposition'] = (
        f'attachment; filename="ccp-job-{job_id}-{job["kind"]}.log"')
    return resp


@app.route('/api/jobs/<int:job_id>', methods=['DELETE'])
@require('admin')
def api_delete_job(job_id):
    """Delete one job and its log file; a running job is refused."""
    if not executor.delete_job(job_id):
        return jsonify({'error': 'the job is still running — wait for it to '
                        'finish (or for the timeout) before deleting it'}), 409
    log_action('job.delete', str(job_id))
    return jsonify({'ok': True})


@app.route('/api/jobs/stats')
def api_jobs_stats():
    """Counts per status and what the job logs occupy on disk."""
    return jsonify(executor.jobs_stats())


@app.route('/api/jobs/cleanup', methods=['POST'])
@require('admin')
def api_jobs_cleanup():
    """Clear the whole job history: every finished job with its log file, plus
    orphan log files. Running jobs are kept (their thread is still writing).
    Returns the counts and fresh stats."""
    res = executor.clear_history()
    if res['deleted'] or res['orphans_removed']:
        log_action('jobs.cleanup', f"{res['deleted']} job(s), {res['orphans_removed']} "
                   f"orphan log(s), {res['bytes_freed']} bytes freed")
    res['stats'] = executor.jobs_stats()
    return jsonify(res)


# ── scripts API ─────────────────────────────────────────────────────────────

@app.route('/api/scripts', methods=['POST'])
@require('operator')
def api_save_script():
    d = request.get_json(force=True) or {}
    name = (d.get('name') or '').strip()
    kind = 'playbook' if d.get('kind') == 'playbook' else 'shell'
    if kind == 'playbook':
        # deprecated: playbooks are developed outside CCP and consumed from
        # filesystem sources (CCP_ANSIBLE_DIRS). Existing rows stay runnable.
        return jsonify({'error': 'saving playbooks in CCP is deprecated — put '
                        'them in an Ansible source directory instead '
                        '(see the Ansible page)'}), 400
    if not name:
        return jsonify({'error': 'name is required'}), 400
    now, who = int(time.time()), session['username']
    existing = db.query('SELECT id FROM scripts WHERE name=?', (name,), one=True)
    if existing:
        db.execute('UPDATE scripts SET kind=?, description=?, content=?, updated_at=?, updated_by=? WHERE id=?',
                   (kind, d.get('description', ''), d.get('content', ''), now, who, existing['id']))
        sid = existing['id']
    else:
        sid = db.execute('INSERT INTO scripts (name, kind, description, content, updated_at, updated_by) '
                         'VALUES (?,?,?,?,?,?)',
                         (name, kind, d.get('description', ''), d.get('content', ''), now, who))
    log_action('script.save', name)
    return jsonify({'id': sid}), 201


@app.route('/api/scripts/<int:sid>', methods=['DELETE'])
@require('operator')
def api_delete_script(sid):
    db.execute('DELETE FROM scripts WHERE id=?', (sid,))
    log_action('script.delete', str(sid))
    return jsonify({'ok': True})


# ── users API ───────────────────────────────────────────────────────────────

@app.route('/api/users', methods=['POST'])
@require('admin')
def api_add_user():
    d = request.get_json(force=True) or {}
    username = (d.get('username') or '').strip()
    password = d.get('password') or ''
    role = d.get('role') if d.get('role') in ROLES else 'viewer'
    if not username or len(password) < 8:
        return jsonify({'error': 'username required and password must be >= 8 chars'}), 400
    # the username becomes an on-disk directory name — enforce path safety here
    if not USERNAME_RE.fullmatch(username):
        return jsonify({'error': 'username must start with a letter or digit and '
                        'contain only letters, digits, dot, dash, underscore '
                        '(max 32 chars)'}), 400
    quota_mb = d.get('quota_mb')
    if quota_mb is not None:
        try:
            quota_mb = int(quota_mb)
            assert quota_mb >= 0
        except (TypeError, ValueError, AssertionError):
            return jsonify({'error': 'quota_mb must be a non-negative integer'}), 400
    try:
        uid = db.execute('INSERT INTO users (username, password_hash, role, quota_mb, created_at) '
                         'VALUES (?,?,?,?,?)',
                         (username, generate_password_hash(password), role, quota_mb,
                          int(time.time())))
    except Exception:
        return jsonify({'error': 'username already exists'}), 400
    log_action('user.add', f'{username} ({role})')
    return jsonify({'id': uid}), 201


@app.route('/api/users/<int:uid>', methods=['DELETE'])
@require('admin')
def api_delete_user(uid):
    if uid == session.get('uid'):
        return jsonify({'error': 'cannot delete your own account'}), 400
    if db.query('SELECT COUNT(*) AS c FROM users')[0]['c'] <= 1:
        return jsonify({'error': 'cannot delete the last user'}), 400
    row = db.query('SELECT username FROM users WHERE id=?', (uid,), one=True)
    if not row:
        return jsonify({'error': 'not found'}), 404
    db.execute('DELETE FROM users WHERE id=?', (uid,))
    # Park the file space out of the way instead of deleting it. Leading '_'
    # can never collide with a real user root (USERNAME_RE requires a leading
    # alphanumeric), and a recreated same-name account starts empty instead of
    # inheriting the previous owner's files.
    if USERNAME_RE.fullmatch(row['username']):
        d = FILES_DIR / row['username']
        if d.is_dir() and not d.is_symlink():
            d.rename(FILES_DIR / f"_removed-{row['username']}-{int(time.time())}")
    log_action('user.delete', f"{uid} ({row['username']})")
    return jsonify({'ok': True})


# ── files API — per-user isolated storage ────────────────────────────────────
# Layout: FILES_DIR/<username>/<subfolders...>/<file>
#
# Trust model / traversal defense (layered — each layer alone would suffice):
#   1. The storage root is derived ONLY from session['username'] (set at login
#      from the DB); no request data ever chooses whose space is touched.
#   2. Usernames and every client-supplied path segment must match a strict
#      allowlist regex — '.', '..', hidden names, separators, NUL bytes and
#      non-ASCII are unrepresentable, so traversal can't even be expressed.
#   3. After joining, the path is resolved (symlinks flattened) and must still
#      be inside the resolved user root, else 403.
#   4. Listings skip symlinks, so nothing outside the tree is ever revealed.

def _user_root(username=None):
    """This user's storage root, auto-created on first use.

    `username` defaults to the server-side session value. It is validated
    again here even though user creation already enforces USERNAME_RE, so a
    legacy/hand-edited DB row can never yield an unsafe directory name."""
    username = username if username is not None else (session.get('username') or '')
    if not USERNAME_RE.fullmatch(username):
        abort(403, description='account name is not valid for file storage')
    root = FILES_DIR / username
    root.mkdir(parents=True, exist_ok=True)
    root = root.resolve()
    base = FILES_DIR.resolve()
    if root == base or not root.is_relative_to(base):
        abort(403, description='storage root escapes the files directory')
    return root


def _safe_rel(raw):
    """Parse a client-supplied path relative to the user root.

    Returns PurePosixPath() for the root. Aborts 400 on anything that is not
    a plain chain of allowlisted segments — so '.', '..', absolute paths,
    backslashes, hidden names and over-deep trees never reach the fs layer."""
    raw = (raw or '').strip()
    if raw.startswith('/'):
        abort(400, description='path must be relative to your file space')
    raw = raw.rstrip('/')
    if not raw:
        return pathlib.PurePosixPath()
    if len(raw) > MAX_RELPATH_LEN or '\\' in raw or '\x00' in raw:
        abort(400, description='invalid path')
    parts = raw.split('/')
    if len(parts) > MAX_TREE_DEPTH:
        abort(400, description=f'folders may nest at most {MAX_TREE_DEPTH} deep')
    for seg in parts:
        if not SEGMENT_RE.fullmatch(seg):
            abort(400, description='path segments may contain only letters, '
                  'digits, dot, dash, underscore and must not start with a dot')
    return pathlib.PurePosixPath(*parts)


def _inside(root, rel):
    """Join a validated rel path onto the user root and re-verify containment
    after resolving symlinks. Belt and braces on top of _safe_rel."""
    p = (root / rel).resolve()
    if not p.is_relative_to(root):
        abort(403, description='path escapes your file space')
    return p


def _tree_size(root):
    """Total bytes stored under a user root (symlinks never followed)."""
    total = 0
    for base, _dirs, names in os.walk(root):   # followlinks=False by default
        for n in names:
            try:
                st = os.lstat(os.path.join(base, n))
            except OSError:
                continue
            total += st.st_size
    return total


def _user_quota_bytes(uid=None):
    """Effective quota in bytes for a user (0 = unlimited)."""
    uid = uid if uid is not None else session.get('uid')
    row = db.query('SELECT quota_mb FROM users WHERE id=?', (uid,), one=True)
    mb = row['quota_mb'] if row and row['quota_mb'] is not None else USER_QUOTA_MB
    return max(0, int(mb)) * 1024 * 1024


def _reap_partials(root):
    """Delete .part temp files older than a day (crashed/aborted uploads)."""
    cutoff = time.time() - 86400
    for base, _dirs, names in os.walk(root):
        for n in names:
            if n.startswith('.') and n.endswith('.part'):
                fp = pathlib.Path(base) / n
                try:
                    if fp.lstat().st_mtime < cutoff:
                        fp.unlink()
                except OSError:
                    pass


@app.route('/api/files', methods=['POST'])
def api_upload():
    f = request.files.get('file')
    if f is None or not f.filename:
        return jsonify({'error': 'no file part'}), 400
    filename = secure_filename(f.filename)
    if not filename or not SEGMENT_RE.fullmatch(filename):
        return jsonify({'error': 'invalid filename — use letters, digits, '
                        'dot, dash, underscore (max 64 chars)'}), 400
    rel = _safe_rel(request.form.get('folder'))
    root = _user_root()
    _reap_partials(root)
    target_dir = _inside(root, rel)
    try:
        target_dir.mkdir(parents=True, exist_ok=True)   # auto-create subfolder
    except (FileExistsError, NotADirectoryError):
        return jsonify({'error': 'a file already exists where that folder '
                        'should be'}), 409
    target = target_dir / filename
    if target.is_dir():
        return jsonify({'error': 'a folder with that name already exists'}), 409

    quota = _user_quota_bytes()
    declared = request.content_length or 0
    used = _tree_size(root)
    replaced = target.stat().st_size if target.is_file() else 0
    if quota and declared and used - replaced + declared > quota + 4096:
        return jsonify({'error': 'upload would exceed your storage quota'}), 413

    tmp = target_dir / f'.{filename}.{secrets.token_hex(6)}.part'
    try:
        f.save(str(tmp))
        size = tmp.stat().st_size
        if quota and used + size - replaced > quota:
            tmp.unlink(missing_ok=True)
            return jsonify({'error': 'upload would exceed your storage quota'}), 413
        tmp.replace(target)
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        return jsonify({'error': f'upload failed: {exc}'}), 500

    relpath = str(rel / filename)
    db.execute('INSERT INTO files (owner_id, relpath, size, uploaded_at) '
               'VALUES (?,?,?,?) ON CONFLICT(owner_id, relpath) '
               'DO UPDATE SET size=excluded.size, uploaded_at=excluded.uploaded_at',
               (session['uid'], relpath, size, int(time.time())))
    log_action('file.upload', relpath)
    return jsonify({'name': filename, 'path': relpath, 'size': size}), 201


@app.route('/api/files/mkdir', methods=['POST'])
def api_mkdir():
    d = request.get_json(force=True) or {}
    rel = _safe_rel(d.get('folder'))
    name = (d.get('name') or '').strip()
    if not SEGMENT_RE.fullmatch(name):
        return jsonify({'error': 'folder name may contain only letters, digits, '
                        'dot, dash, underscore and must not start with a dot'}), 400
    if len(rel.parts) >= MAX_TREE_DEPTH:
        return jsonify({'error': f'folders may nest at most {MAX_TREE_DEPTH} deep'}), 400
    root = _user_root()
    new = _inside(root, rel / name)
    if new.is_file():
        return jsonify({'error': 'a file with that name already exists'}), 409
    try:
        new.mkdir(parents=True, exist_ok=True)
    except (FileExistsError, NotADirectoryError):
        return jsonify({'error': 'cannot create folder there'}), 409
    log_action('folder.create', str(rel / name))
    return jsonify({'path': str(rel / name)}), 201


@app.route('/api/files/move', methods=['POST'])
def api_move():
    """Move or rename a file or folder within the caller's own space.
    Body: {src, dst}. Both are validated and containment-checked; the move can
    never cross into another user's root because both resolve under _user_root()."""
    d = request.get_json(force=True) or {}
    src = _safe_rel(d.get('src'))
    dst = _safe_rel(d.get('dst'))
    if not src.parts:
        return jsonify({'error': 'src is required'}), 400
    if not dst.parts:
        return jsonify({'error': 'dst is required'}), 400
    if src == dst:
        return jsonify({'error': 'src and dst are the same'}), 400
    root = _user_root()
    sp = _inside(root, src)
    dp = _inside(root, dst)
    if not sp.exists():
        return jsonify({'error': 'not found'}), 404
    was_file = sp.is_file()
    if not was_file and (dst == src or str(dst).startswith(str(src) + '/')):
        return jsonify({'error': 'cannot move a folder into itself'}), 400
    if dp.exists():
        return jsonify({'error': 'destination already exists'}), 409
    dp.parent.mkdir(parents=True, exist_ok=True)
    sp.rename(dp)
    if was_file:
        db.execute('UPDATE files SET relpath=? WHERE owner_id=? AND relpath=?',
                   (str(dst), session['uid'], str(src)))
    else:
        rows = db.query('SELECT id, relpath FROM files WHERE owner_id=? '
                        'AND (relpath=? OR relpath LIKE ?)',
                        (session['uid'], str(src), str(src) + '/%'))
        for r in rows:
            newrel = str(dst) + r['relpath'][len(str(src)):]
            db.execute('UPDATE files SET relpath=? WHERE id=?', (newrel, r['id']))
    log_action('file.move', f'{src} -> {dst}')
    return jsonify({'ok': True, 'from': str(src), 'to': str(dst)})


@app.route('/files/download/<path:relpath>')
def download_file(relpath):
    rel = _safe_rel(relpath)
    if not rel.parts:
        abort(404)
    p = _inside(_user_root(), rel)
    if not p.is_file():
        abort(404)
    log_action('file.download', str(rel))
    # as_attachment + global nosniff header: uploads are never rendered inline,
    # so an uploaded .html/.svg cannot execute in the panel's origin.
    return send_file(p, as_attachment=True, download_name=p.name)


@app.route('/api/files/<path:relpath>', methods=['DELETE'])
def api_delete_file(relpath):
    rel = _safe_rel(relpath)
    if not rel.parts:
        return jsonify({'error': 'not found'}), 404
    p = _inside(_user_root(), rel)
    if not p.is_file():
        return jsonify({'error': 'not found'}), 404
    p.unlink()
    db.execute('DELETE FROM files WHERE owner_id=? AND relpath=?',
               (session['uid'], str(rel)))
    log_action('file.delete', str(rel))
    return jsonify({'ok': True})


@app.route('/api/folders/<path:relpath>', methods=['DELETE'])
def api_delete_folder(relpath):
    rel = _safe_rel(relpath)
    if not rel.parts:
        return jsonify({'error': 'cannot delete your root folder'}), 400
    p = _inside(_user_root(), rel)
    if not p.is_dir():
        return jsonify({'error': 'not found'}), 404
    try:
        p.rmdir()                      # only empty folders — no recursive rm
    except OSError:
        return jsonify({'error': 'folder is not empty'}), 409
    log_action('folder.delete', str(rel))
    return jsonify({'ok': True})


@app.errorhandler(413)
def _too_large(_e):
    return jsonify({'error': 'file exceeds the upload size limit'}), 413


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
def _err(e):
    """abort(...) inside the path helpers should yield JSON on API routes."""
    if request.path.startswith('/api/'):
        return jsonify({'error': e.description or e.name}), e.code
    return e


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('CCP_PORT', '8060')), debug=False)
