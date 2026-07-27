import os
import sys
import time
import secrets
import datetime
from urllib.parse import quote

from flask import (Flask, render_template, jsonify, request, session,
                   redirect, url_for, Response, flash)
from werkzeug.security import generate_password_hash, check_password_hash
import docker
import requests

app = Flask(__name__)

LEASES_FILE      = os.environ.get('LEASES_FILE', '/data/dnsmasq.leases')
REFRESH_INTERVAL = int(os.environ.get('REFRESH_INTERVAL', '30'))
SESSION_MINUTES  = int(os.environ.get('MONITOR_SESSION_MINUTES', '30'))

# Where dashboard file uploads are forwarded. The iPXE Manager owns the file
# share (safe filenames, ISO kernel/initrd extraction, auto boot entries), so
# the monitor proxies to it instead of writing to the share directly — one
# upload path, identical behavior. Default is the compose-internal DNS name.
IPXE_MANAGER_URL      = os.environ.get('IPXE_MANAGER_URL',
                                       'http://ipxe-manager:8091').rstrip('/')
IPXE_MANAGER_PASSWORD = os.environ.get('IPXE_MANAGER_PASSWORD', '')
# Files uploaded through this dashboard land in their own space inside the
# share: data/webfs_share/<MONITOR_UPLOAD_DIR>/ (URLs under /files/<dir>/).
MONITOR_UPLOAD_DIR    = os.environ.get('MONITOR_UPLOAD_DIR', 'monitor')

app.config.update(
    SECRET_KEY=os.environ.get('MONITOR_SECRET_KEY') or secrets.token_hex(32),
    PERMANENT_SESSION_LIFETIME=SESSION_MINUTES * 60,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_REFRESH_EACH_REQUEST=True,  # sliding: each request resets the 30-min clock
)

# ports for the dashboard quick-links (the browser fills in the host)
LINKS = [
    {'label': 'iPXE Manager',  'port': os.environ.get('IPXE_MANAGER_PORT', '8091'), 'icon': '☁'},
    {'label': 'File server',   'port': os.environ.get('WEBFS_PORT', '8080'),        'icon': '\U0001F4C1', 'path': 'files/'},
    {'label': 'Control Panel', 'port': os.environ.get('CCP_PORT', '8060'),          'icon': '⚙'},
]

# ── users / roles ─────────────────────────────────────────────────────────────
# Login is required for the whole dashboard; accounts carry a role (admin or a
# read-only viewer) shown in the top bar. Passwords are hashed once at startup.
def _load_users():
    """Build the account table from the environment."""
    users = {}
    au = os.environ.get('MONITOR_ADMIN_USER', 'admin')
    ap = os.environ.get('MONITOR_ADMIN_PASSWORD', '')
    if ap:
        users[au] = {'hash': generate_password_hash(ap), 'role': 'admin'}
    vu = os.environ.get('MONITOR_VIEWER_USER', 'viewer')
    vp = os.environ.get('MONITOR_VIEWER_PASSWORD', '')
    if vp:
        users[vu] = {'hash': generate_password_hash(vp), 'role': 'viewer'}
    return users


USERS = _load_users()
if not USERS:
    sys.stderr.write('[monitor] WARNING: no MONITOR_ADMIN_PASSWORD set — login is '
                     'impossible. Set it in .env and recreate the container.\n')
    sys.stderr.flush()


def _client_ip():
    return (request.headers.get('X-Forwarded-For', '').split(',')[0].strip()
            or request.remote_addr or '')


def audit(action, detail=''):
    sys.stderr.write(f'[monitor][audit] {session.get("user", "-")} {action} {detail} '
                     f'from {_client_ip()}\n')
    sys.stderr.flush()


PUBLIC_PATHS = {'/login', '/healthz', '/favicon.ico'}


@app.before_request
def _guard():
    if request.path in PUBLIC_PATHS or request.path.startswith('/static/'):
        return None
    if not session.get('user'):
        if request.path.startswith('/api/'):
            return jsonify({'error': 'authentication required'}), 401
        return redirect(url_for('login', next=request.path))
    if request.method in ('POST', 'PUT', 'DELETE', 'PATCH'):
        token = request.headers.get('X-CSRF-Token')
        if not token and request.mimetype == 'application/x-www-form-urlencoded':
            # Only look inside urlencoded bodies for the token. Parsing a
            # multipart body here would spool the whole upload to disk AND
            # consume the stream /api/upload must forward — the proxied POST
            # would then hang forever waiting for body bytes that never come.
            token = request.form.get('_csrf')
        if not token or token != session.get('csrf'):
            return jsonify({'error': 'invalid or missing CSRF token'}), 403
    return None


@app.context_processor
def _inject():
    return {'cur_user': session.get('user'), 'cur_role': session.get('role'),
            'csrf_token': session.get('csrf'), 'session_minutes': SESSION_MINUTES}


# ── data (dashboard) ──────────────────────────────────────────────────────────

def _uptime_str(started_at: str) -> str:
    if not started_at or started_at.startswith('0001'):
        return '-'
    try:
        ts = started_at[:19]
        dt = datetime.datetime.strptime(ts, '%Y-%m-%dT%H:%M:%S').replace(
            tzinfo=datetime.timezone.utc)
        secs = int((datetime.datetime.now(datetime.timezone.utc) - dt).total_seconds())
        if secs < 60:
            return f'{secs}s'
        if secs < 3600:
            return f'{secs // 60}m {secs % 60}s'
        if secs < 86400:
            return f'{secs // 3600}h {(secs % 3600) // 60}m'
        return f'{secs // 86400}d {(secs % 86400) // 3600}h'
    except Exception:
        return '-'


def get_containers():
    try:
        client = docker.from_env()
        result = []
        for c in client.containers.list(all=True):
            state  = c.attrs.get('State', {})
            health = state.get('Health', {}).get('Status', 'none')
            try:
                image = (c.image.tags[0] if c.image and c.image.tags else
                         c.image.short_id if c.image else '(unknown)')
            except Exception:
                image = '(image removed)'
            result.append({
                'name':     c.name,
                'status':   c.status,
                'health':   health,
                'uptime':   _uptime_str(state.get('StartedAt', '')),
                'restarts': c.attrs.get('RestartCount', 0),
                'image':    image,
            })
        result.sort(key=lambda x: (0 if x['status'] == 'running' else 1, x['name']))
        return result, None
    except Exception as exc:
        return [], str(exc)


def get_leases():
    leases = []
    if not os.path.exists(LEASES_FILE):
        return leases, f'Leases file not found: {LEASES_FILE}'
    try:
        now = int(time.time())
        with open(LEASES_FILE) as fh:
            for line in fh:
                try:
                    parts = line.strip().split()
                    if len(parts) < 4:
                        continue
                    try:
                        expiry_ts = int(parts[0])
                    except ValueError:
                        continue
                    mac       = parts[1].upper()
                    ip        = parts[2]
                    hostname  = parts[3] if parts[3] != '*' else '(unknown)'
                    remaining = expiry_ts - now
                    if expiry_ts == 0:
                        # dnsmasq writes 0 for "infinite" leases (static hosts
                        # or PXE_LEASE_TIME=infinite) — they never expire
                        expires_str   = 'Never'
                        remaining_str = 'Infinite'
                        remaining     = 1
                    elif remaining <= 0:
                        expires_str   = 'Expired'
                        remaining_str = 'Expired'
                    else:
                        expires_str   = datetime.datetime.fromtimestamp(expiry_ts).strftime('%Y-%m-%d %H:%M')
                        h, m          = divmod(remaining // 60, 60)
                        remaining_str = f'{h}h {m}m'
                    octets = ip.split('.')
                    sort_key = [int(o) for o in octets] if (
                        len(octets) == 4 and all(o.isdigit() for o in octets)) else [999]
                    leases.append({
                        'ip': ip, 'mac': mac, 'hostname': hostname,
                        'expires': expires_str, 'remaining': remaining_str,
                        'expired': remaining <= 0, 'sort_key': sort_key,
                    })
                except Exception:
                    continue
        leases.sort(key=lambda x: x['sort_key'])
        return leases, None
    except Exception as exc:
        return [], str(exc)


# ── auth routes ───────────────────────────────────────────────────────────────

@app.route('/healthz')
def healthz():
    return Response('ok', mimetype='text/plain')


@app.route('/favicon.ico')
def favicon():
    # without this every browser visit logs a 404 for the tab icon
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
           '<circle cx="8" cy="8" r="7" fill="none" stroke="#38bdf8" stroke-width="2"/>'
           '<circle cx="8" cy="8" r="3" fill="#38bdf8"/></svg>')
    return Response(svg, mimetype='image/svg+xml',
                    headers={'Cache-Control': 'public, max-age=86400'})


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        rec = USERS.get(username)
        if rec and check_password_hash(rec['hash'], password):
            session.clear()
            session['user'] = username
            session['role'] = rec['role']
            session['csrf'] = secrets.token_hex(16)
            session.permanent = True
            audit('login', 'success')
            nxt = request.args.get('next', '')
            return redirect(nxt if nxt.startswith('/') else url_for('dashboard'))
        audit('login', f'failed user={username!r}')
        flash('Invalid username or password.')
    return render_template('login.html')


@app.route('/logout', methods=['POST'])
def logout():
    audit('logout')
    session.clear()
    return redirect(url_for('login'))


# ── pages ─────────────────────────────────────────────────────────────────────

@app.route('/')
def dashboard():
    containers, c_err = get_containers()
    leases,     l_err = get_leases()
    return render_template(
        'index.html', containers=containers, leases=leases, c_err=c_err, l_err=l_err,
        now=datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        refresh=REFRESH_INTERVAL, links=LINKS, monitor_space=MONITOR_UPLOAD_DIR)


# ── APIs ──────────────────────────────────────────────────────────────────────

@app.route('/api/status')
def api_status():
    containers, _ = get_containers()
    leases,     _ = get_leases()
    return jsonify({'timestamp': int(time.time()), 'containers': containers, 'leases': leases})


class _KnownLengthStream:
    """Adapter so requests emits an exact Content-Length for the proxied body
    (it sizes file-like bodies via `.len`; without it the upload would be sent
    chunked). The body still streams through in blocks, never buffered whole."""
    def __init__(self, stream, length):
        self._stream = stream
        self.len = length

    def read(self, *args):
        return self._stream.read(*args)

    def __iter__(self):  # requests only treats objects with __iter__ as streams
        return iter(lambda: self._stream.read(65536), b'')


def _mgr_session():
    """HTTP session for monitor→manager calls on the compose network.
    trust_env=False: a Docker daemon that injects corporate HTTP(S)_PROXY
    variables into containers would otherwise send this internal request to
    the proxy, which cannot resolve 'ipxe-manager' — the call would hang or
    fail even though both containers are healthy."""
    s = requests.Session()
    s.trust_env = False
    if IPXE_MANAGER_PASSWORD:
        s.auth = ('monitor', IPXE_MANAGER_PASSWORD)
    return s


def _mgr_error(exc):
    msg = f'iPXE Manager unreachable from the monitor container: {exc}'
    sys.stderr.write(f'[monitor][upload] {msg}\n')
    sys.stderr.flush()
    return msg


@app.route('/api/upload', methods=['POST'])
def api_upload():
    # the viewer role is read-only by contract — uploads are for admins
    if session.get('role') != 'admin':
        return jsonify({'error': 'admin role required'}), 403
    # Stream the browser's multipart body through to the iPXE Manager
    # untouched (never buffered in RAM — request.form/request.files are never
    # touched here, and the CSRF guard only parses the form when the
    # X-CSRF-Token header is missing, which the dashboard always sends).
    body = request.stream
    if request.content_length:
        body = _KnownLengthStream(body, request.content_length)
    headers = {'Content-Type': request.content_type or 'application/octet-stream'}
    try:
        # ?dir= puts dashboard uploads in the monitor's own space in the share
        resp = _mgr_session().post(
            f'{IPXE_MANAGER_URL}/api/files?dir={quote(MONITOR_UPLOAD_DIR)}',
            data=body, headers=headers, timeout=(10, 3600))
    except requests.RequestException as exc:
        return jsonify({'error': _mgr_error(exc)}), 502
    try:
        name = resp.json().get('name', '?') if resp.ok else f'(HTTP {resp.status_code})'
    except Exception:
        name = f'(HTTP {resp.status_code})'
    audit('upload', name)
    return Response(resp.content, resp.status_code, mimetype='application/json')


@app.route('/api/files')
def api_files():
    """File-server listing for the dashboard's File Server section (all
    logged-in roles — the viewer sees it read-only)."""
    try:
        resp = _mgr_session().get(f'{IPXE_MANAGER_URL}/api/files', timeout=10)
    except requests.RequestException as exc:
        return jsonify({'error': _mgr_error(exc)}), 502
    return Response(resp.content, resp.status_code, mimetype='application/json')


@app.route('/api/files/<path:name>', methods=['DELETE'])
def api_delete_file(name):
    # removal is a state change — admins only, like uploads
    if session.get('role') != 'admin':
        return jsonify({'error': 'admin role required'}), 403
    try:
        resp = _mgr_session().delete(f'{IPXE_MANAGER_URL}/api/files/{quote(name)}',
                                     timeout=30)
    except requests.RequestException as exc:
        return jsonify({'error': _mgr_error(exc)}), 502
    audit('delete_file', f'{name} (HTTP {resp.status_code})')
    return Response(resp.content, resp.status_code, mimetype='application/json')


@app.route('/api/upload/check')
def api_upload_check():
    """Preflight for the dashboard upload card: proves the monitor container
    can reach AND authenticate to the iPXE Manager, so a broken link shows up
    on the card at page load instead of as a dead upload. The same reason is
    written to the container log (docker logs lab_monitor)."""
    if session.get('role') != 'admin':
        return jsonify({'ok': False, 'error': 'admin role required'}), 403
    try:
        r = _mgr_session().get(f'{IPXE_MANAGER_URL}/api/config', timeout=5)
    except requests.RequestException as exc:
        return jsonify({'ok': False, 'error': _mgr_error(exc)})
    if r.status_code == 401:
        return jsonify({'ok': False, 'error':
                        'iPXE Manager rejected the password — set the same '
                        'IPXE_MANAGER_PASSWORD for both containers in .env, then '
                        'run: docker compose up -d monitor ipxe-manager'})
    if not r.ok:
        return jsonify({'ok': False,
                        'error': f'iPXE Manager answered HTTP {r.status_code}'})
    return jsonify({'ok': True})


if __name__ == '__main__':
    import os
    os.execvp('gunicorn', ['gunicorn', '-w', '2', '-k', 'gthread', '--threads', '8',
                           '--timeout', '120', '-b', '0.0.0.0:8090', 'app:app'])
