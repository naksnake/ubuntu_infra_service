"""Local-filesystem Ansible sources.

CCP is not a development environment: playbooks and roles are written outside
(VS Code, git checkouts, /root/playbooks, …) and consumed here read-only.
CCP_ANSIBLE_DIRS is a colon-separated list of container paths (bind-mount the
host directories in docker-compose); each existing dir is a "source" that is
scanned for playbooks, roles, inventories and group_vars/host_vars. Execution
runs ansible-playbook with the playbook's directory as cwd so sibling roles/
and vars resolve exactly as they do when run by hand.
"""
import os
import re
import pathlib

ANSIBLE_DIRS = [d for d in os.environ.get(
    'CCP_ANSIBLE_DIRS', '/data/ccp/ansible').split(':') if d.strip()]

MAX_SCAN_DEPTH = 4          # playbook nesting below a source root
_SKIP_DIRS = {'roles', 'group_vars', 'host_vars', 'inventories', 'inventory',
              'collections', 'files', 'templates', 'tasks', 'handlers',
              'vars', 'defaults', 'meta', 'library', '.git'}
_HOSTS_RE = re.compile(r'^\s*(-\s+)?hosts\s*:', re.MULTILINE)


def ensure_default_dirs():
    """Create configured source dirs where possible (the default lives inside
    the /data/ccp bind mount, so operators see ./data/ccp/ansible appear on
    the host and can drop playbooks in). Missing permissions are not fatal —
    the dir is simply not listed until it exists."""
    for d in ANSIBLE_DIRS:
        try:
            pathlib.Path(d).mkdir(parents=True, exist_ok=True)
        except OSError:
            pass


def sources():
    """Configured source dirs that actually exist, resolved."""
    out = []
    for d in ANSIBLE_DIRS:
        p = pathlib.Path(d)
        if p.is_dir():
            out.append(str(p.resolve()))
    return out


def _looks_like_playbook(path):
    """Cheap heuristic: a YAML file whose head contains a 'hosts:' key. Reads
    at most 16 KB so a scan never chokes on a big vars file."""
    try:
        with open(path, 'r', errors='replace') as fh:
            head = fh.read(16384)
    except OSError:
        return False
    return bool(_HOSTS_RE.search(head))


def scan(source):
    """Inventory one source dir. Returns {source, playbooks, roles,
    inventories} with paths relative to the source root."""
    root = pathlib.Path(source)
    playbooks, inventories = [], []
    for base, dirs, names in os.walk(root):
        rel_base = pathlib.Path(base).relative_to(root)
        depth = len(rel_base.parts)
        # never descend into role/vars internals or VCS metadata; cap depth
        dirs[:] = [d for d in sorted(dirs)
                   if d not in _SKIP_DIRS and not d.startswith('.')
                   and depth < MAX_SCAN_DEPTH]
        for n in sorted(names):
            if n.startswith('.'):
                continue
            rel = str(rel_base / n) if rel_base.parts else n
            full = os.path.join(base, n)
            if n.endswith(('.yml', '.yaml')) and _looks_like_playbook(full):
                playbooks.append(rel)
            elif n.endswith(('.ini', '.inv')) or n == 'hosts':
                inventories.append(rel)
    playbooks.sort()
    inventories.sort()
    roles = []
    roles_dir = root / 'roles'
    if roles_dir.is_dir():
        roles = sorted(d.name for d in roles_dir.iterdir()
                       if d.is_dir() and not d.name.startswith('.'))
    return {'source': str(root), 'playbooks': playbooks, 'roles': roles,
            'inventories': inventories}


def scan_all():
    return [scan(s) for s in sources()]


def resolve_playbook(source, relpath):
    """Validate a (source, relpath) pair from a client. Returns the absolute
    playbook path or raises ValueError. Source must be one of the configured
    dirs verbatim; relpath is containment-checked after resolution (same
    layered defense as the files API)."""
    if source not in sources():
        raise ValueError('unknown ansible source')
    rel = (relpath or '').strip()
    if (not rel or rel.startswith('/') or '\\' in rel or '\x00' in rel
            or len(rel) > 512 or not rel.endswith(('.yml', '.yaml'))):
        raise ValueError('invalid playbook path')
    if any(seg in ('.', '..') or seg.startswith('.') for seg in rel.split('/')):
        raise ValueError('invalid playbook path')
    root = pathlib.Path(source).resolve()
    full = (root / rel).resolve()
    if not full.is_relative_to(root):
        raise ValueError('playbook path escapes the source')
    if not full.is_file():
        raise ValueError('playbook not found')
    return str(full)
