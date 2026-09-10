"""Hostname-driven topology: hostnames like rack0_sled1_gpu ARE the topology.

rack/sled/role are derived by parsing, never entered by hand — there is no
rack database to maintain. Nodes whose names don't match the scheme still work
everywhere; they simply don't place in the rack view.

Recognized shapes (case-insensitive, '_' or '-' separators):
    rack<N>_sled<M>_<role>     rack0_sled1_gpu
    rack<N>-sled<M>-<role>     rack12-sled3-cpu
    rack<N>_sled<M>            rack1_sled4        (role empty)
"""
import re

import db

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


def apply(node_id, name):
    """Recompute and store the topology columns for a node. Call whenever a
    node's name is set or changed (add, import, onboarding rename, hostname
    job) so the inventory and rack view stay consistent."""
    t = parse(name)
    db.execute('UPDATE nodes SET rack=?, sled=?, role=? WHERE id=?',
               (t['rack'], t['sled'], t['role'], node_id))
    return t


def backfill(conn):
    """One-time migration helper: parse every existing node name using an
    explicit connection (init_db runs before the per-thread pool is used)."""
    for row in conn.execute('SELECT id, name FROM nodes').fetchall():
        t = parse(row['name'])
        conn.execute('UPDATE nodes SET rack=?, sled=?, role=? WHERE id=?',
                     (t['rack'], t['sled'], t['role'], row['id']))
    conn.commit()
