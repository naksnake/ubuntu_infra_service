"""Cluster Control Panel (CCP) — a lightweight, self-hosted control panel for a
PXE lab: run shell commands across nodes with ClusterShell, run Ansible
playbooks, keep a script repository, browse job history and audit logs, and
share files — all behind session login with role-based access control.

Roles (increasing privilege): viewer < operator < admin
  viewer   — read-only: nodes, jobs + output, scripts, download files
  operator — viewer + run jobs, manage nodes/scripts, upload/delete files
  admin    — operator + manage users, view audit log, delete jobs
"""
import os
import json
import time
import functools
import secrets
import pathlib

from flask import (Flask, request, session, redirect, url_for, render_template,
                   jsonify, Response, abort, send_from_directory, flash)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

import db
import executor

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get('CCP_SECRET_KEY') or secrets.token_hex(32),
    MAX_CONTENT_LENGTH=int(os.environ.get('CCP_MAX_UPLOAD_MB', '2048')) * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    PERMANENT_SESSION_LIFETIME=int(os.environ.get('CCP_SESSION_HOURS', '12')) * 3600,
)

FILES_DIR = pathlib.Path(os.environ.get('CCP_FILES_DIR', '/data/ccp/files'))
ROLES = ('viewer', 'operator', 'admin')
_RANK = {r: i for i, r in enumerate(ROLES)}

db.init_db()


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


@app.route('/healthz')
def healthz():
    return Response('ok', mimetype='text/plain')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        row = db.query('SELECT * FROM users WHERE username=?', (username,), one=True)
        if row and check_password_hash(row['password_hash'], password):
            session.clear()
            session['uid'] = row['id']
            session['username'] = row['username']
            session['role'] = row['role']
            session['csrf'] = secrets.token_hex(16)
            session.permanent = True
            db.audit(username, 'login', 'success', _client_ip())
            nxt = request.args.get('next', '')
            return redirect(nxt if nxt.startswith('/') else url_for('dashboard'))
        db.audit(username or '-', 'login', 'failed', _client_ip())
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
    nodes = db.query('SELECT COUNT(*) AS c FROM nodes')[0]['c']
    scripts = db.query('SELECT COUNT(*) AS c FROM scripts')[0]['c']
    jobs = db.query('SELECT COUNT(*) AS c FROM jobs')[0]['c']
    running = db.query("SELECT COUNT(*) AS c FROM jobs WHERE status='running'")[0]['c']
    recent = db.query('SELECT * FROM jobs ORDER BY id DESC LIMIT 8')
    return render_template('dashboard.html', stats={
        'nodes': nodes, 'scripts': scripts, 'jobs': jobs, 'running': running},
        recent=recent)


@app.route('/nodes')
def nodes_page():
    return render_template('nodes.html', nodes=db.query('SELECT * FROM nodes ORDER BY name'))


@app.route('/shell')
def shell_page():
    return render_template('shell.html',
                           nodes=db.query('SELECT * FROM nodes ORDER BY name'),
                           scripts=db.query("SELECT * FROM scripts WHERE kind='shell' ORDER BY name"))


@app.route('/ansible')
def ansible_page():
    return render_template('ansible.html',
                           nodes=db.query('SELECT * FROM nodes ORDER BY name'),
                           scripts=db.query("SELECT * FROM scripts WHERE kind='playbook' ORDER BY name"))


@app.route('/jobs')
def jobs_page():
    return render_template('jobs.html',
                           jobs=db.query('SELECT * FROM jobs ORDER BY id DESC LIMIT 200'))


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
    return render_template('files.html', files=_list_files())


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

@app.route('/api/nodes', methods=['POST'])
@require('operator')
def api_add_node():
    d = request.get_json(force=True) or {}
    name = (d.get('name') or '').strip()
    address = (d.get('address') or '').strip()
    if not name or not address:
        return jsonify({'error': 'name and address are required'}), 400
    conn = 'local' if d.get('conn') == 'local' else 'ssh'
    try:
        node_id = db.execute(
            'INSERT INTO nodes (name, address, conn, ssh_user, ssh_port, groups, created_at) '
            'VALUES (?,?,?,?,?,?,?)',
            (name, address, conn, (d.get('ssh_user') or 'root').strip(),
             int(d.get('ssh_port') or 22), (d.get('groups') or '').strip(), int(time.time())))
    except Exception as exc:
        return jsonify({'error': f'could not add node: {exc}'}), 400
    log_action('node.add', name)
    return jsonify({'id': node_id}), 201


@app.route('/api/nodes/<int:node_id>', methods=['DELETE'])
@require('operator')
def api_delete_node(node_id):
    db.execute('DELETE FROM nodes WHERE id=?', (node_id,))
    log_action('node.delete', str(node_id))
    return jsonify({'ok': True})


# ── job launch API ────────────────────────────────────────────────────────────

def _selected_nodes(d):
    ids = [int(x) for x in d.get('node_ids', []) if str(x).isdigit()]
    group = (d.get('group') or '').strip()
    if group:
        for r in db.query('SELECT id, groups FROM nodes'):
            if group in [g.strip() for g in (r['groups'] or '').split(',') if g.strip()]:
                ids.append(r['id'])
    ids = sorted(set(ids))
    names = [r['name'] for r in db.query('SELECT name FROM nodes WHERE id IN (%s)'
             % ','.join('?' for _ in ids), tuple(ids))] if ids else []
    return ids, names


@app.route('/api/run/shell', methods=['POST'])
@require('operator')
def api_run_shell():
    d = request.get_json(force=True) or {}
    command = (d.get('command') or '').strip()
    if not command:
        return jsonify({'error': 'command is required'}), 400
    ids, names = _selected_nodes(d)
    if not ids:
        return jsonify({'error': 'select at least one node or a group'}), 400
    job_id = executor.start_job('shell', ','.join(names),
                                {'node_ids': ids, 'command': command},
                                session['username'])
    log_action('run.shell', f'job {job_id}: {command[:120]}')
    return jsonify({'job_id': job_id}), 201


@app.route('/api/run/ansible', methods=['POST'])
@require('operator')
def api_run_ansible():
    d = request.get_json(force=True) or {}
    playbook = d.get('playbook') or ''
    if not playbook.strip():
        return jsonify({'error': 'playbook content is required'}), 400
    ids, names = _selected_nodes(d)
    if not ids:
        return jsonify({'error': 'select at least one node or a group'}), 400
    job_id = executor.start_job('ansible', ','.join(names),
                                {'node_ids': ids, 'playbook': playbook,
                                 'extra_vars': (d.get('extra_vars') or '').strip()},
                                session['username'])
    log_action('run.ansible', f'job {job_id}')
    return jsonify({'job_id': job_id}), 201


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


@app.route('/api/jobs/<int:job_id>', methods=['DELETE'])
@require('admin')
def api_delete_job(job_id):
    db.execute('DELETE FROM jobs WHERE id=?', (job_id,))
    log_action('job.delete', str(job_id))
    return jsonify({'ok': True})


# ── scripts API ─────────────────────────────────────────────────────────────

@app.route('/api/scripts', methods=['POST'])
@require('operator')
def api_save_script():
    d = request.get_json(force=True) or {}
    name = (d.get('name') or '').strip()
    kind = 'playbook' if d.get('kind') == 'playbook' else 'shell'
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
    try:
        uid = db.execute('INSERT INTO users (username, password_hash, role, created_at) VALUES (?,?,?,?)',
                         (username, generate_password_hash(password), role, int(time.time())))
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
    db.execute('DELETE FROM users WHERE id=?', (uid,))
    log_action('user.delete', str(uid))
    return jsonify({'ok': True})


# ── files API ───────────────────────────────────────────────────────────────

def _list_files():
    out = []
    if FILES_DIR.exists():
        for f in sorted(FILES_DIR.iterdir()):
            if f.is_file() and not f.name.startswith('.'):
                st = f.stat()
                out.append({'name': f.name, 'size': st.st_size, 'mtime': int(st.st_mtime)})
    return out


@app.route('/api/files', methods=['POST'])
@require('operator')
def api_upload():
    if 'file' not in request.files:
        return jsonify({'error': 'no file part'}), 400
    f = request.files['file']
    filename = secure_filename(f.filename or '')
    if not filename:
        return jsonify({'error': 'invalid filename'}), 400
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    tmp = FILES_DIR / f'.{filename}.{secrets.token_hex(6)}.part'
    try:
        f.save(str(tmp))
        tmp.replace(FILES_DIR / filename)
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        return jsonify({'error': f'upload failed: {exc}'}), 500
    log_action('file.upload', filename)
    return jsonify({'name': filename}), 201


@app.route('/files/download/<path:filename>')
def download_file(filename):
    safe = secure_filename(filename)
    if not safe or not (FILES_DIR / safe).is_file():
        abort(404)
    log_action('file.download', safe)
    return send_from_directory(FILES_DIR, safe, as_attachment=True)


@app.route('/api/files/<path:filename>', methods=['DELETE'])
@require('operator')
def api_delete_file(filename):
    safe = secure_filename(filename)
    p = FILES_DIR / safe
    if safe and p.is_file():
        p.unlink()
        log_action('file.delete', safe)
        return jsonify({'ok': True})
    return jsonify({'error': 'not found'}), 404


@app.errorhandler(413)
def _too_large(_e):
    return jsonify({'error': 'file exceeds the upload size limit'}), 413


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('CCP_PORT', '8060')), debug=False)
