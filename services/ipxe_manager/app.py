import os
import json
import uuid
import pathlib

from flask import Flask, request, jsonify, render_template, Response
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 8 * 1024 * 1024 * 1024  # 8 GB

UPLOAD_DIR   = pathlib.Path(os.environ.get('UPLOAD_DIR',   '/data/uploads'))
ENTRIES_FILE = pathlib.Path(os.environ.get('ENTRIES_FILE', '/data/entries.json'))
WEBFS_BASE   = os.environ.get('WEBFS_BASE',  'http://192.168.100.1:8080')
SERVER_IP    = os.environ.get('SERVER_IP',   '192.168.100.1')

# ── persistence ──────────────────────────────────────────────────────────────

def load_entries():
    if not ENTRIES_FILE.exists() or ENTRIES_FILE.stat().st_size == 0:
        return []
    try:
        return json.loads(ENTRIES_FILE.read_text())
    except Exception:
        return []

def save_entries(entries):
    ENTRIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = ENTRIES_FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps(entries, indent=2))
    tmp.rename(ENTRIES_FILE)

# ── file helpers ─────────────────────────────────────────────────────────────

def list_files():
    result = []
    if UPLOAD_DIR.exists():
        for f in sorted(UPLOAD_DIR.iterdir()):
            if f.is_file():
                result.append({
                    'name': f.name,
                    'size': f.stat().st_size,
                    'url':  f'{WEBFS_BASE}/files/{f.name}',
                })
    return result

# ── iPXE menu generator ───────────────────────────────────────────────────────

def generate_menu(entries):
    enabled = [e for e in entries if e.get('enabled', True)]
    default = enabled[0]['id'] if enabled else 'shell'

    lines = [
        '#!ipxe',
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
            kernel = e.get('kernel', '')
            initrd = e.get('initrd', '')
            cmdline = e.get('cmdline', '')
            lines.append(f'kernel {WEBFS_BASE}/files/{kernel}')
            if initrd:
                lines.append(f'initrd {WEBFS_BASE}/files/{initrd}')
            if cmdline:
                lines.append(f'imgargs {kernel} {cmdline}')
            lines.append('boot || shell')
        elif t == 'iso':
            iso = e.get('iso', '')
            lines.append(f'sanboot {WEBFS_BASE}/files/{iso} || shell')
        elif t == 'chain':
            lines.append(f'chain {e.get("url", "")} || shell')

    lines += [
        '\n:shell',
        'shell',
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
    f.save(str(dest))
    return jsonify({'name': filename, 'size': dest.stat().st_size,
                    'url': f'{WEBFS_BASE}/files/{filename}'}), 201

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
    data = request.get_json(force=True) or {}
    entry = {
        'id':      uuid.uuid4().hex[:8],
        'name':    data.get('name', 'Unnamed'),
        'type':    data.get('type', 'kernel'),
        'enabled': data.get('enabled', True),
    }
    for k in ('kernel', 'initrd', 'cmdline', 'iso', 'url'):
        if k in data:
            entry[k] = data[k]
    entries = load_entries()
    entries.append(entry)
    save_entries(entries)
    return jsonify(entry), 201

@app.route('/api/entries/<eid>', methods=['PUT'])
def api_update_entry(eid):
    data = request.get_json(force=True) or {}
    entries = load_entries()
    for i, e in enumerate(entries):
        if e['id'] == eid:
            entries[i] = {**e, **data, 'id': eid}
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
    save_entries(reordered)
    return jsonify({'ok': True})

if __name__ == '__main__':
    import subprocess
    subprocess.execvp('gunicorn', [
        'gunicorn', '-w', '2', '--timeout', '120',
        '-b', '0.0.0.0:8091', 'app:app',
    ])
