import os
import re
import json
import uuid
import pathlib
from urllib.parse import quote

from flask import Flask, request, jsonify, render_template, Response
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 8 * 1024 * 1024 * 1024  # 8 GB

UPLOAD_DIR    = pathlib.Path(os.environ.get('UPLOAD_DIR',   '/data/uploads'))
ENTRIES_FILE  = pathlib.Path(os.environ.get('ENTRIES_FILE', '/data/state/entries.json'))
WEBFS_BASE    = os.environ.get('WEBFS_BASE', 'http://192.168.100.1:8080')
SERVER_IP     = os.environ.get('SERVER_IP',  '192.168.100.1')
AUTH_PASSWORD = os.environ.get('AUTH_PASSWORD', '')

# ── optional auth (everything except the PXE-facing menu endpoint) ──────────

@app.before_request
def _require_auth():
    if not AUTH_PASSWORD or request.path == '/menu.ipxe':
        return None
    auth = request.authorization
    if auth and auth.password == AUTH_PASSWORD:
        return None
    return Response('Authentication required.', 401,
                    {'WWW-Authenticate': 'Basic realm="iPXE Manager"'})

# ── persistence ──────────────────────────────────────────────────────────────

def load_entries():
    if not ENTRIES_FILE.exists() or ENTRIES_FILE.stat().st_size == 0:
        return []
    try:
        return json.loads(ENTRIES_FILE.read_text())
    except Exception:
        return []

def save_entries(entries):
    # ENTRIES_FILE lives on a directory bind mount, so tmp+rename is atomic
    # and visible on the host (renaming onto a single-file mount would EBUSY).
    ENTRIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = ENTRIES_FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps(entries, indent=2))
    tmp.replace(ENTRIES_FILE)

# ── input sanitization ───────────────────────────────────────────────────────

_CTRL = re.compile(r'[\x00-\x1f\x7f]')

def _clean(value, maxlen=200):
    # iPXE scripts are newline-delimited: control chars in any field would
    # let a client inject arbitrary boot directives into menu.ipxe.
    return _CTRL.sub(' ', str(value)).strip()[:maxlen]

def sanitize_fields(data):
    """Whitelist, clean and validate entry fields. Returns (fields, error)."""
    out = {}
    if 'name' in data:
        name = _clean(data['name'])
        if not name:
            return None, 'name must not be empty'
        out['name'] = name
    if 'type' in data:
        if data['type'] not in ('kernel', 'iso', 'chain'):
            return None, 'type must be kernel, iso or chain'
        out['type'] = data['type']
    if 'enabled' in data:
        out['enabled'] = bool(data['enabled'])
    for k in ('kernel', 'initrd', 'iso'):
        if k in data:
            fn = secure_filename(str(data[k])) if data[k] else ''
            if data[k] and not fn:
                return None, f'invalid {k} filename'
            out[k] = fn
    if 'cmdline' in data:
        out['cmdline'] = _clean(data['cmdline'], 500)
    if 'url' in data:
        url = _clean(data['url'], 500)
        if url and not re.fullmatch(r'https?://\S+', url):
            return None, 'url must be http(s):// with no spaces'
        out['url'] = url
    return out, None

# ── file helpers ─────────────────────────────────────────────────────────────

def file_url(name):
    return f'{WEBFS_BASE}/files/{quote(name)}'

def list_files():
    result = []
    if UPLOAD_DIR.exists():
        for f in sorted(UPLOAD_DIR.iterdir()):
            # dotfiles include .<name>.uploading partials — never show them
            if f.is_file() and not f.name.startswith('.'):
                result.append({
                    'name': f.name,
                    'size': f.stat().st_size,
                    'url':  file_url(f.name),
                })
    return result

# ── iPXE menu generator ───────────────────────────────────────────────────────

def generate_menu(entries):
    enabled = [e for e in entries if e.get('enabled', True)]
    default = enabled[0]['id'] if enabled else 'shell'

    lines = [
        '#!ipxe',
        ':start',
        'menu Lab PXE Boot Menu',
        'item --gap -- ---- Boot Options ----',
    ]
    for e in enabled:
        lines.append(f"item {e['id']:<12} {e['name']}")
    lines += [
        'item --gap --',
        'item shell        iPXE Shell',
        'item exit         Exit to BIOS/UEFI',
        f'choose --default {default} --timeout 30000 target && goto ${{target}} || goto exit',
    ]

    for e in enabled:
        lines.append(f'\n:{e["id"]}')
        t = e.get('type', 'kernel')
        if t == 'kernel':
            kernel  = e.get('kernel', '')
            initrd  = e.get('initrd', '')
            cmdline = e.get('cmdline', '')
            # args go on the kernel line so no imgargs name-matching is needed
            lines.append(f'kernel {file_url(kernel)}' + (f' {cmdline}' if cmdline else ''))
            if initrd:
                lines.append(f'initrd {file_url(initrd)}')
            lines.append('boot || goto failed')
        elif t == 'iso':
            lines.append(f'sanboot {file_url(e.get("iso", ""))} || goto failed')
        elif t == 'chain':
            lines.append(f'chain {e.get("url", "")} || goto failed')
        # a returned/failed boot must not fall through into the next section
        lines.append('goto start')

    lines += [
        '\n:failed',
        'echo Boot failed — returning to menu in 5 seconds',
        'sleep 5',
        'goto start',
        '\n:shell',
        "echo Type 'exit' to return to the menu",
        'shell',
        'goto start',
        '\n:exit',
        'exit',
    ]
    return '\n'.join(lines) + '\n'

# ── routes ───────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html', webfs_base=WEBFS_BASE, server_ip=SERVER_IP)

@app.route('/menu.ipxe')
def menu_ipxe():
    return Response(generate_menu(load_entries()), mimetype='text/plain')

@app.route('/api/preview', methods=['GET'])
def api_preview():
    return Response(generate_menu(load_entries()), mimetype='text/plain')

# files

@app.route('/api/files', methods=['GET'])
def api_files():
    return jsonify(list_files())

@app.route('/api/files', methods=['POST'])
def api_upload():
    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400
    f = request.files['file']
    filename = secure_filename(f.filename or '')
    if not filename:
        return jsonify({'error': 'Invalid filename'}), 400
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    dest = UPLOAD_DIR / filename
    # stream to a hidden temp name so webfs never serves a half-written file,
    # then rename into place on success
    tmp = UPLOAD_DIR / f'.{filename}.uploading'
    try:
        f.save(str(tmp))
        tmp.replace(dest)
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        return jsonify({'error': f'Upload failed: {exc}'}), 500
    return jsonify({'name': filename, 'size': dest.stat().st_size,
                    'url': file_url(filename)}), 201

@app.route('/api/files/<filename>', methods=['DELETE'])
def api_delete_file(filename):
    path = UPLOAD_DIR / secure_filename(filename)
    if path.exists() and path.is_file():
        path.unlink()
        return jsonify({'ok': True})
    return jsonify({'error': 'Not found'}), 404

# boot entries

@app.route('/api/entries', methods=['GET'])
def api_get_entries():
    return jsonify(load_entries())

@app.route('/api/entries', methods=['POST'])
def api_add_entry():
    fields, err = sanitize_fields(request.get_json(force=True) or {})
    if err:
        return jsonify({'error': err}), 400
    if not fields.get('name'):
        return jsonify({'error': 'name is required'}), 400
    # ids double as iPXE goto labels; leading letter keeps them unambiguous
    entry = {'id': 'e' + uuid.uuid4().hex[:7], 'type': 'kernel', 'enabled': True}
    entry.update(fields)
    entries = load_entries()
    entries.append(entry)
    save_entries(entries)
    return jsonify(entry), 201

@app.route('/api/entries/<eid>', methods=['PUT'])
def api_update_entry(eid):
    fields, err = sanitize_fields(request.get_json(force=True) or {})
    if err:
        return jsonify({'error': err}), 400
    entries = load_entries()
    for i, e in enumerate(entries):
        if e['id'] == eid:
            entries[i] = {**e, **fields, 'id': eid}
            save_entries(entries)
            return jsonify(entries[i])
    return jsonify({'error': 'Not found'}), 404

@app.route('/api/entries/<eid>', methods=['DELETE'])
def api_delete_entry(eid):
    save_entries([e for e in load_entries() if e['id'] != eid])
    return jsonify({'ok': True})

@app.route('/api/entries/reorder', methods=['POST'])
def api_reorder():
    order = (request.get_json(force=True) or {}).get('order', [])
    entries = load_entries()
    by_id = {e['id']: e for e in entries}
    reordered = [by_id[eid] for eid in order if eid in by_id]
    # entries the client didn't know about (added concurrently) must survive
    listed = set(order)
    reordered += [e for e in entries if e['id'] not in listed]
    save_entries(reordered)
    return jsonify({'ok': True})

if __name__ == '__main__':
    import subprocess
    # gthread: heartbeat stays in the main thread, so multi-GB uploads in
    # worker threads are not killed by the arbiter timeout (sync workers are)
    subprocess.execvp('gunicorn', [
        'gunicorn', '-w', '2', '-k', 'gthread', '--threads', '8',
        '--timeout', '120', '-b', '0.0.0.0:8091', 'app:app',
    ])
