import os
import sys
import time
import secrets
import datetime

from flask import (Flask, render_template, jsonify, request, session,
                   redirect, url_for, Response, flash)
from werkzeug.security import generate_password_hash, check_password_hash
import docker

app = Flask(__name__)

LEASES_FILE      = os.environ.get('LEASES_FILE', '/data/dnsmasq.leases')
REFRESH_INTERVAL = int(os.environ.get('REFRESH_INTERVAL', '30'))
SESSION_MINUTES  = int(os.environ.get('MONITOR_SESSION_MINUTES', '30'))

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


PUBLIC_PATHS = {'/login', '/healthz'}


@app.before_request
def _guard():
    if request.path in PUBLIC_PATHS or request.path.startswith('/static/'):
        return None
    if not session.get('user'):
        if request.path.startswith('/api/'):
            return jsonify({'error': 'authentication required'}), 401
        return redirect(url_for('login', next=request.path))
    if request.method in ('POST', 'PUT', 'DELETE', 'PATCH'):
        token = request.headers.get('X-CSRF-Token') or request.form.get('_csrf')
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
                    if remaining <= 0:
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
        refresh=REFRESH_INTERVAL, links=LINKS)


# ── APIs ──────────────────────────────────────────────────────────────────────

@app.route('/api/status')
def api_status():
    containers, _ = get_containers()
    leases,     _ = get_leases()
    return jsonify({'timestamp': int(time.time()), 'containers': containers, 'leases': leases})


if __name__ == '__main__':
    import os
    os.execvp('gunicorn', ['gunicorn', '-w', '2', '-k', 'gthread', '--threads', '8',
                           '--timeout', '120', '-b', '0.0.0.0:8090', 'app:app'])
