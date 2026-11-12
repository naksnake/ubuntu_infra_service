"""Rack layout files: import physical rack positions from Excel, JSON or YAML.

Hostname parsing (topology.py) is the default source of rack/sled/role, but it
only works when every machine can carry a rack-encoded name. Labs usually
already have the cabling plan as a spreadsheet, so CCP can also take that
plan as a file. A layout row places a machine by MAC (preferred — it never
changes), hostname or IP address, and may also name the hostname the machine
should end up with; the Discovery import then uses that name, and onboarding
sets it on the box.

Precedence when a node is placed (see topology.resolve):
    layout row matched by MAC  >  by hostname  >  by IP  >  hostname parsing

Accepted files (≤ MAX_FILE_BYTES, ≤ MAX_ROWS rows):
    .xlsx / .xlsm   first sheet (or one named layout/rack/racks/nodes); the
                    first row holding known column names is the header
    .csv            header row + rows (Excel "Save as CSV")
    .json / .yaml   a list of rows, {"nodes": [...]}, or grouped by rack:
                    {"racks": [{"rack": 0, "nodes": [{"sled": 1, ...}]}]}
                    {"racks": {"rack0": [{"sled": 1, ...}]}}

Columns (case-insensitive, aliases in _ALIASES): hostname, mac, ip, rack,
sled, role, notes. Each row needs a rack and at least one of hostname / mac /
ip. Unknown columns are ignored with a warning. An import with any invalid
row changes nothing — the whole file must be clean.
"""
import csv
import io
import json
import re
import time

import yaml

import db

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_ROWS = 5000
FIELDS = ('hostname', 'mac', 'address', 'rack', 'sled', 'role', 'notes')
EXTENSIONS = ('.xlsx', '.xlsm', '.csv', '.json', '.yaml', '.yml')

_ALIASES = {
    'hostname': ('hostname', 'host_name', 'host', 'name', 'node', 'node_name', 'nodename',
                 'planned_hostname', 'target_hostname', 'server'),
    'mac': ('mac', 'mac_address', 'macaddress', 'mac_addr', 'hwaddr', 'pxe_mac'),
    'address': ('ip', 'address', 'ip_address', 'ipaddress', 'ipaddr', 'ipv4', 'addr'),
    'rack': ('rack', 'rack_id', 'rack_no', 'rack_number', 'rack_num', 'cabinet'),
    'sled': ('sled', 'slot', 'sled_no', 'slot_no', 'position', 'pos', 'u',
             'unit', 'rack_unit', 'node_slot'),
    'role': ('role', 'type', 'node_type', 'kind', 'function'),
    'notes': ('notes', 'note', 'comment', 'comments', 'description', 'remark',
              'remarks'),
}
_HEADER = {alias: field for field, names in _ALIASES.items() for alias in names}

_NAME_RE = re.compile(r'[A-Za-z0-9._-]{1,63}')        # same as app.NODE_NAME_RE
_ADDR_RE = re.compile(r'[A-Za-z0-9._:-]{1,255}')      # same as app.NODE_ADDR_RE
_ROLE_RE = re.compile(r'[A-Za-z0-9._-]{1,32}')
# lenient numbers: 3, 3.0 (Excel), "3", "rack3", "Rack 03", "R3", "sled-2", "U12"
_NUM_RE = {
    'rack': re.compile(r'(?:rack|r)?[\s_-]*0*(\d{1,4})', re.I),
    'sled': re.compile(r'(?:sled|slot|pos(?:ition)?|s|u)?[\s_-]*0*(\d{1,4})', re.I),
}


class LayoutError(ValueError):
    """The file as a whole cannot be read (wrong type, corrupt, too big)."""


# ── normalisation ────────────────────────────────────────────────────────────

def norm_mac(value):
    """'00-1a-2b-3c-4d-5e' / '001a.2b3c.4d5e' / '001A2B3C4D5E' →
    '00:1A:2B:3C:4D:5E'. '' for empty, None when it is not a 48-bit MAC."""
    s = re.sub(r'[\s:.-]', '', str(value or '')).upper()
    if not s:
        return ''
    if not re.fullmatch(r'[0-9A-F]{12}', s):
        return None
    return ':'.join(s[i:i + 2] for i in range(0, 12, 2))


def _norm_header(h):
    return re.sub(r'[^a-z0-9]+', '_', str(h or '').strip().lower()).strip('_')


def _text(v):
    if v is None:
        return ''
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def _number(v, field):
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if isinstance(v, bool):
        raise ValueError(f'{field} must be a number')
    if isinstance(v, int):
        n = v
    elif isinstance(v, float):
        if not v.is_integer():
            raise ValueError(f'{field} must be a whole number (got {v})')
        n = int(v)
    else:
        m = _NUM_RE[field].fullmatch(str(v).strip())
        if not m:
            raise ValueError(f'{field} {str(v)[:40]!r} is not a number')
        n = int(m.group(1))
    if not 0 <= n <= 9999:
        raise ValueError(f'{field} must be 0-9999')
    return n


def normalize_row(raw):
    """Map one raw record onto FIELDS. Returns (row, error, unknown_keys)."""
    vals, unknown = {}, set()
    for k, v in raw.items():
        field = _HEADER.get(_norm_header(k))
        if field is None:
            if _text(k):
                unknown.add(_text(k))
            continue
        if field not in vals or vals[field] in (None, ''):
            vals[field] = v
    try:
        rack = _number(vals.get('rack'), 'rack')
        sled = _number(vals.get('sled'), 'sled')
    except ValueError as exc:
        return None, str(exc), unknown
    hostname = _text(vals.get('hostname'))
    address = _text(vals.get('address'))
    role = _text(vals.get('role')).lower()
    notes = _text(vals.get('notes'))[:200]
    mac = norm_mac(_text(vals.get('mac')))
    if mac is None:
        return None, f'mac {_text(vals.get("mac"))[:40]!r} is not a valid MAC address', unknown
    if rack is None:
        return None, 'rack is required', unknown
    if not (hostname or mac or address):
        return None, 'needs at least one of hostname, mac or ip to identify the machine', unknown
    if hostname and not _NAME_RE.fullmatch(hostname):
        return None, (f'hostname {hostname[:63]!r} may contain only letters, '
                      'digits, dot, dash, underscore (max 63)'), unknown
    if address and not _ADDR_RE.fullmatch(address):
        return None, f'ip {address[:60]!r} contains invalid characters', unknown
    if role and not _ROLE_RE.fullmatch(role):
        return None, f'role {role[:32]!r} may contain only letters, digits, dot, dash, underscore', unknown
    return {'hostname': hostname, 'mac': mac, 'address': address, 'rack': rack,
            'sled': sled, 'role': role, 'notes': notes}, None, unknown


# ── readers: every reader yields (label, raw_dict) ────────────────────────────

def _records_from_doc(doc):
    """JSON/YAML document → [(label, dict)] for the accepted shapes."""
    if isinstance(doc, dict):
        if 'racks' in doc:
            return _records_from_racks(doc['racks'])
        for key in ('nodes', 'layout', 'rows', 'servers'):
            if key in doc:
                doc = doc[key]
                break
        else:
            raise LayoutError('expected a list of rows, or a "nodes" or "racks" key')
    if not isinstance(doc, list):
        raise LayoutError('expected a list of rows')
    out = []
    for i, item in enumerate(doc, 1):
        out.append((f'item {i}', item))
    return out


def _records_from_racks(racks):
    if isinstance(racks, dict):
        racks = [{'rack': k, 'nodes': v} for k, v in racks.items()]
    if not isinstance(racks, list):
        raise LayoutError('"racks" must be a list or a mapping')
    out = []
    for ri, rack in enumerate(racks, 1):
        if not isinstance(rack, dict):
            raise LayoutError(f'racks entry {ri} must be a mapping')
        nodes = rack.get('nodes', rack.get('sleds', rack.get('servers')))
        if not isinstance(nodes, list):
            raise LayoutError(f'racks entry {ri} needs a "nodes" list')
        rack_id = rack.get('rack', rack.get('name', rack.get('id')))
        for ni, item in enumerate(nodes, 1):
            if isinstance(item, dict):
                item = dict(item)
                if not any(_HEADER.get(_norm_header(k)) == 'rack' for k in item):
                    item['rack'] = rack_id
            out.append((f'rack {_text(rack_id) or ri}, node {ni}', item))
    return out


def _records_from_xlsx(data):
    try:
        import openpyxl
    except ImportError as exc:                       # pragma: no cover
        raise LayoutError('Excel support needs the openpyxl package') from exc
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        raise LayoutError(f'not a readable .xlsx workbook ({type(exc).__name__})') from exc
    try:
        sheets = wb.worksheets
        ws = next((s for s in sheets if s.title.strip().lower()
                   in ('layout', 'rack', 'racks', 'nodes', 'rack layout')), sheets[0])
        header, out = None, []
        for rno, cells in enumerate(ws.iter_rows(values_only=True), 1):
            if header is None:
                if rno > 20:
                    break
                names = [_norm_header(c) for c in cells]
                if sum(1 for n in names if n in _HEADER) >= 2:
                    header = [_text(c) for c in cells]
                continue
            if all(_text(c) == '' for c in cells):
                continue
            if len(out) >= MAX_ROWS:
                raise LayoutError(f'too many rows (max {MAX_ROWS})')
            out.append((f'row {rno}', {header[i]: c for i, c in enumerate(cells)
                                       if i < len(header) and header[i]}))
        if header is None:
            raise LayoutError(f'sheet "{ws.title}" has no header row — expected '
                              'columns such as hostname, mac, ip, rack, sled, role')
        return out
    finally:
        wb.close()


def _records_from_csv(data):
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise LayoutError('CSV must be UTF-8 encoded') from exc
    reader = csv.DictReader(io.StringIO(text))
    out = []
    for item in reader:
        if all(_text(v) == '' for v in item.values()):
            continue
        out.append((f'row {reader.line_num}',
                    {k: v for k, v in item.items() if k is not None}))
    return out


def parse(filename, data):
    """Read and validate a layout file. Returns (rows, errors, warnings):
    rows are normalised dicts (FIELDS + 'label'); errors/warnings are
    human-readable strings. Raises LayoutError for unreadable files."""
    if len(data) > MAX_FILE_BYTES:
        raise LayoutError(f'file too large (max {MAX_FILE_BYTES // (1024 * 1024)} MB)')
    ext = ('.' + filename.rsplit('.', 1)[-1].lower()) if '.' in (filename or '') else ''
    if ext == '.xls':
        raise LayoutError('legacy .xls is not supported — save the sheet as .xlsx or .csv')
    if ext in ('.xlsx', '.xlsm'):
        records = _records_from_xlsx(data)
    elif ext == '.csv':
        records = _records_from_csv(data)
    elif ext in ('.json', '.yaml', '.yml'):
        try:
            text = data.decode('utf-8-sig')
            doc = json.loads(text) if ext == '.json' else yaml.safe_load(text)
        except (UnicodeDecodeError, ValueError, yaml.YAMLError) as exc:
            raise LayoutError(f'could not parse {ext[1:].upper()}: '
                              f'{str(exc).splitlines()[0][:200]}') from exc
        if doc is None:
            raise LayoutError('the file is empty')
        records = _records_from_doc(doc)
    else:
        raise LayoutError('unsupported file type — use .xlsx, .csv, .json or .yaml')
    if len(records) > MAX_ROWS:
        raise LayoutError(f'too many rows (max {MAX_ROWS})')

    rows, errors, unknown = [], [], set()
    for label, raw in records:
        if not isinstance(raw, dict):
            errors.append(f'{label}: expected a mapping of column → value')
            continue
        row, err, unk = normalize_row(raw)
        unknown |= unk
        if err:
            errors.append(f'{label}: {err}')
            continue
        row['label'] = label
        rows.append(row)
    errors += _duplicate_errors(rows)
    warnings = []
    if unknown:
        warnings.append('ignored columns: ' + ', '.join(sorted(unknown)[:20]))
    if not rows and not errors:
        errors.append('no layout rows found in the file')
    return rows, errors, warnings


def _duplicate_errors(rows):
    errors, seen = [], {}
    for r in rows:
        keys = []
        if r['hostname']:
            keys.append(('hostname', r['hostname'].lower(), r['hostname']))
        if r['mac']:
            keys.append(('mac', r['mac'], r['mac']))
        if r['address']:
            keys.append(('ip', r['address'], r['address']))
        if r['sled'] is not None:
            keys.append(('slot', (r['rack'], r['sled']),
                         f'rack {r["rack"]} sled {r["sled"]}'))
        for kind, key, shown in keys:
            first = seen.setdefault((kind, key), r['label'])
            if first != r['label']:
                errors.append(f'{r["label"]}: {kind} {shown} is already used by {first}')
    return errors


# ── storage ──────────────────────────────────────────────────────────────────

def load():
    rows = db.query('SELECT * FROM rack_layout ORDER BY rack, sled, hostname, id')
    return [dict(r) for r in rows]


def meta():
    n = db.query('SELECT COUNT(*) AS c FROM rack_layout', one=True)['c']
    last = db.query('SELECT imported_at, imported_by, source FROM rack_layout '
                    'ORDER BY imported_at DESC, id DESC LIMIT 1', one=True)
    return {'rows': n, 'imported_at': last['imported_at'] if last else None,
            'imported_by': last['imported_by'] if last else '',
            'source': last['source'] if last else ''}


def save(rows, mode, username, source=''):
    """Store parsed rows atomically. mode='replace' swaps the whole layout;
    'merge' replaces only the existing rows that share a hostname, MAC, IP or
    rack/sled slot with an incoming row and keeps the rest."""
    conn = db.get_db()
    now = int(time.time())
    try:
        if mode == 'replace':
            conn.execute('DELETE FROM rack_layout')
        for r in rows:
            if mode == 'merge':
                conn.execute(
                    "DELETE FROM rack_layout WHERE "
                    "(?<>'' AND lower(hostname)=lower(?)) OR (?<>'' AND mac=?) OR "
                    "(?<>'' AND address=?) OR (? IS NOT NULL AND rack=? AND sled=?)",
                    (r['hostname'], r['hostname'], r['mac'], r['mac'],
                     r['address'], r['address'], r['sled'], r['rack'], r['sled']))
            conn.execute(
                'INSERT INTO rack_layout (hostname, mac, address, rack, sled, role, '
                'notes, source, imported_at, imported_by) VALUES (?,?,?,?,?,?,?,?,?,?)',
                (r['hostname'], r['mac'], r['address'], r['rack'], r['sled'],
                 r['role'], r['notes'], source[:120], now, username or ''))
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def clear():
    db.execute('DELETE FROM rack_layout')


# ── matching ─────────────────────────────────────────────────────────────────

def index(rows=None):
    rows = load() if rows is None else rows
    idx = {'mac': {}, 'hostname': {}, 'address': {}}
    for r in rows:
        if r['mac']:
            idx['mac'].setdefault(r['mac'], r)
        if r['hostname']:
            idx['hostname'].setdefault(r['hostname'].lower(), r)
        if r['address']:
            idx['address'].setdefault(r['address'], r)
    return idx


def match(idx, name='', mac='', address=''):
    """The layout row describing a machine, or None. MAC wins over hostname,
    hostname over IP (DHCP addresses churn; MACs don't)."""
    m = norm_mac(mac) or ''
    return ((m and idx['mac'].get(m))
            or (name and idx['hostname'].get(name.lower()))
            or (address and idx['address'].get(address))
            or None)


# ── export / templates ───────────────────────────────────────────────────────

def _export_rows(rows):
    out = []
    for r in rows:
        item = {'hostname': r['hostname'], 'mac': r['mac'], 'ip': r['address'],
                'rack': r['rack'], 'sled': r['sled'], 'role': r['role'],
                'notes': r['notes']}
        out.append({k: v for k, v in item.items() if v not in ('', None)})
    return out


EXAMPLE_ROWS = [
    {'hostname': 'rack0-sled1-gpu', 'mac': '00:1A:2B:3C:4D:01', 'address': '192.168.100.21',
     'rack': 0, 'sled': 1, 'role': 'gpu', 'notes': 'HGX node'},
    {'hostname': 'rack0-sled2-gpu', 'mac': '00:1A:2B:3C:4D:02', 'address': '',
     'rack': 0, 'sled': 2, 'role': 'gpu', 'notes': ''},
    {'hostname': '', 'mac': '00:1A:2B:3C:4D:11', 'address': '',
     'rack': 1, 'sled': 1, 'role': 'cpu', 'notes': 'placed by MAC only'},
]

YAML_TEMPLATE = """\
# CCP rack layout. Each machine is identified by mac (preferred), hostname or ip.
# hostname, when given, is the name CCP gives the machine on Discovery import.
# Columns: hostname, mac, ip, rack (required), sled, role, notes
racks:
  - rack: 0
    nodes:
      - {sled: 1, hostname: rack0-sled1-gpu, mac: "00:1A:2B:3C:4D:01", ip: 192.168.100.21, role: gpu, notes: HGX node}
      - {sled: 2, hostname: rack0-sled2-gpu, mac: "00:1A:2B:3C:4D:02", role: gpu}
  - rack: 1
    nodes:
      - {sled: 1, mac: "00:1A:2B:3C:4D:11", role: cpu, notes: placed by MAC only}
"""


def to_json(rows):
    return json.dumps({'nodes': _export_rows(rows)}, indent=2).encode()


def to_yaml(rows):
    return yaml.safe_dump({'nodes': _export_rows(rows)}, sort_keys=False,
                          default_flow_style=False).encode()


def to_xlsx(rows, with_readme=False):
    import openpyxl
    from openpyxl.styles import Font
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'layout'
    cols = ('hostname', 'mac', 'ip', 'rack', 'sled', 'role', 'notes')
    ws.append(list(cols))
    for c in ws[1]:
        c.font = Font(bold=True)
    for r in rows:
        ws.append([r['hostname'], r['mac'], r['address'], r['rack'], r['sled'],
                   r['role'], r['notes']])
    for col, width in zip('ABCDEFG', (22, 20, 16, 7, 7, 10, 30)):
        ws.column_dimensions[col].width = width
    ws.freeze_panes = 'A2'
    if with_readme:
        rd = wb.create_sheet('README')
        for line in (
                'CCP rack layout — fill the "layout" sheet, then import it on the Rack View page.',
                '',
                'rack      required. 0, 3, "rack3" or "R03" all mean rack 3.',
                'sled      slot / position inside the rack (optional, unique per rack).',
                'mac       preferred identifier (PXE NIC). Any common MAC format.',
                'hostname  planned hostname; Discovery import uses it as the node name.',
                'ip        used only when neither mac nor hostname matches.',
                'role      gpu, cpu, storage, … (letters/digits).',
                'notes     free text.',
                '',
                'Each row needs a rack plus at least one of mac, hostname or ip.',
                'Layout positions take precedence over rack/sled parsed from hostnames.'):
            rd.append([line])
        rd.column_dimensions['A'].width = 100
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


MIMETYPES = {
    'json': 'application/json',
    'yaml': 'application/x-yaml',
    'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
}


def export(rows, fmt):
    return {'json': to_json, 'yaml': to_yaml, 'xlsx': to_xlsx}[fmt](rows)


def template(fmt):
    if fmt == 'yaml':
        return YAML_TEMPLATE.encode()
    if fmt == 'xlsx':
        return to_xlsx(EXAMPLE_ROWS, with_readme=True)
    return to_json(EXAMPLE_ROWS)
