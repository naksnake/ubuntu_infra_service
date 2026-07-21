import os
import time
import datetime
from flask import Flask, render_template, jsonify
import docker

app = Flask(__name__)

LEASES_FILE      = os.environ.get('LEASES_FILE', '/data/dnsmasq.leases')
REFRESH_INTERVAL = int(os.environ.get('REFRESH_INTERVAL', '30'))


def _uptime_str(started_at: str) -> str:
    """Return human-readable uptime from a Docker StartedAt timestamp."""
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
            h = secs // 3600
            return f'{h}h {(secs % 3600) // 60}m'
        d = secs // 86400
        return f'{d}d {(secs % 86400) // 3600}h'
    except Exception:
        return '-'


def get_containers():
    try:
        client = docker.from_env()
        result = []
        for c in client.containers.list(all=True):
            state  = c.attrs.get('State', {})
            health = state.get('Health', {}).get('Status', 'none')
            result.append({
                'name':     c.name,
                'status':   c.status,
                'health':   health,
                'uptime':   _uptime_str(state.get('StartedAt', '')),
                'restarts': c.attrs.get('RestartCount', 0),
                'image':    (c.image.tags[0] if c.image and c.image.tags else
                             c.image.short_id if c.image else '(unknown)'),
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
                leases.append({
                    'ip':        ip,
                    'mac':       mac,
                    'hostname':  hostname,
                    'expires':   expires_str,
                    'remaining': remaining_str,
                    'expired':   remaining <= 0,
                    'sort_key':  [int(p) for p in ip.split('.')],
                })
        leases.sort(key=lambda x: x['sort_key'])
        return leases, None
    except Exception as exc:
        return [], str(exc)


@app.route('/')
def index():
    containers, c_err = get_containers()
    leases,     l_err = get_leases()
    return render_template(
        'index.html',
        containers=containers,
        leases=leases,
        c_err=c_err,
        l_err=l_err,
        now=datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        refresh=REFRESH_INTERVAL,
    )


@app.route('/api/status')
def api_status():
    containers, _ = get_containers()
    leases,     _ = get_leases()
    return jsonify({
        'timestamp':  int(time.time()),
        'containers': containers,
        'leases':     leases,
    })


if __name__ == '__main__':
    # Dev/local convenience only — the container runs gunicorn as its CMD.
    import os
    os.execvp('gunicorn', ['gunicorn', '-w', '2', '-b', '0.0.0.0:8090', 'app:app'])
