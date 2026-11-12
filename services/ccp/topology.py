"""Node topology (rack/sled/role): from an imported rack layout file, else
parsed from the hostname — hostnames like rack0_sled1_gpu ARE the topology.

rack/sled/role are never typed in per node. When a rack layout file has been
imported (rack_layout.py) and one of its rows matches the node by MAC,
hostname or IP, that row wins; otherwise the hostname is parsed. Nodes
placed by neither still work everywhere; they just show as unracked.

Recognized shapes (case-insensitive, '_' or '-' separators):
    rack<N>_sled<M>_<role>     rack0_sled1_gpu
    rack<N>-sled<M>-<role>     rack12-sled3-cpu
    rack<N>_sled<M>            rack1_sled4        (role empty)
"""
import re

import db
import rack_layout

_TOPO_RE = re.compile(
    r'^rack(?P<rack>\d{1,4})[_-]sled(?P<sled>\d{1,4})(?:[_-](?P<role>[A-Za-z0-9]+))?$',
    re.IGNORECASE)


def parse(name):
    """Parse a hostname into topology fields.
    Returns {'rack': int|None, 'sled': int|None, 'role': str}."""
    m = _TOPO_RE.match((name or '').strip())
    if not m:
        return {'rack': None, 'sled': None, 'role': ''}
    return {'rack': int(m.group('rack')), 'sled': int(m.group('sled')),
            'role': (m.group('role') or '').lower()}


def resolve(name, mac='', address='', idx=None):
    """Topology for a node: {'rack', 'sled', 'role', 'source', 'planned_name'}.
    source is 'layout', 'hostname' or '' (unplaced). Pass idx (from
    rack_layout.index()) when resolving many nodes."""
    t = parse(name)
    idx = rack_layout.index() if idx is None else idx
    row = rack_layout.match(idx, name, mac, address)
    if row:
        return {'rack': row['rack'], 'sled': row['sled'],
                'role': row['role'] or t['role'], 'source': 'layout',
                'planned_name': row['hostname'], 'layout_id': row['id']}
    t.update(source='hostname' if t['rack'] is not None else '',
             planned_name='', layout_id=None)
    return t


def apply(node_id, name):
    """Recompute and store the topology columns for a node. Call whenever a
    node's name is set or changed (add, import, onboarding rename, hostname
    job) so the inventory and rack view stay consistent."""
    n = db.query('SELECT mac, address FROM nodes WHERE id=?', (node_id,), one=True)
    t = resolve(name, n['mac'] if n else '', n['address'] if n else '')
    db.execute('UPDATE nodes SET rack=?, sled=?, role=?, topo_source=? WHERE id=?',
               (t['rack'], t['sled'], t['role'], t['source'], node_id))
    return t


def refresh_all():
    """Re-place every node, e.g. after a rack layout import or clear.
    Returns how many nodes are placed by the layout."""
    idx = rack_layout.index()
    conn = db.get_db()
    placed = 0
    for n in conn.execute('SELECT id, name, mac, address FROM nodes').fetchall():
        t = resolve(n['name'], n['mac'], n['address'], idx)
        placed += t['source'] == 'layout'
        conn.execute('UPDATE nodes SET rack=?, sled=?, role=?, topo_source=? WHERE id=?',
                     (t['rack'], t['sled'], t['role'], t['source'], n['id']))
    conn.commit()
    return placed


def backfill(conn):
    """One-time migration helper: parse every existing node name using an
    explicit connection (init_db runs before the per-thread pool is used)."""
    for row in conn.execute('SELECT id, name FROM nodes').fetchall():
        t = parse(row['name'])
        conn.execute('UPDATE nodes SET rack=?, sled=?, role=? WHERE id=?',
                     (t['rack'], t['sled'], t['role'], row['id']))
    conn.commit()
