"""SQLite persistence for the Cluster Control Panel.

One connection per thread (gunicorn gthread workers + background job threads each
get their own). SQLite in WAL mode handles the low write concurrency of a lab
control panel comfortably; the schema is created and the bootstrap admin seeded
on first import.
"""
import os
import sys
import json
import time
import sqlite3
import threading

from werkzeug.security import generate_password_hash

DB_PATH = os.environ.get('CCP_DB', '/data/ccp/ccp.db')

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'viewer',
    created_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS nodes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    address    TEXT NOT NULL,
    conn       TEXT NOT NULL DEFAULT 'ssh',     -- 'ssh' | 'local'
    ssh_user   TEXT NOT NULL DEFAULT 'root',
    ssh_port   INTEGER NOT NULL DEFAULT 22,
    groups     TEXT NOT NULL DEFAULT '',        -- comma-separated
    created_at INTEGER NOT NULL,
    -- lifecycle: discovered | onboarding | managed | failed | unverified.
    -- Only the onboarding/verify job may set 'managed' on an ssh node — and
    -- only after password auth, key install and key-auth execution all pass.
    state        TEXT NOT NULL DEFAULT 'unverified',
    state_detail TEXT NOT NULL DEFAULT '',
    mac          TEXT NOT NULL DEFAULT '',      -- from DHCP discovery, if known
    onboarded_at INTEGER                        -- when the node became managed
);

CREATE TABLE IF NOT EXISTS scripts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    kind        TEXT NOT NULL DEFAULT 'shell',  -- 'shell' | 'playbook'
    description TEXT NOT NULL DEFAULT '',
    content     TEXT NOT NULL DEFAULT '',
    updated_at  INTEGER NOT NULL,
    updated_by  TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,                  -- 'shell' | 'ansible'
    target      TEXT NOT NULL DEFAULT '',       -- human-readable node list
    spec        TEXT NOT NULL DEFAULT '{}',     -- JSON payload
    status      TEXT NOT NULL DEFAULT 'running',-- running | success | failed
    exit_code   INTEGER,
    created_by  TEXT NOT NULL DEFAULT '',
    created_at  INTEGER NOT NULL,
    finished_at INTEGER
);

-- Metadata for uploaded files. The filesystem remains the source of truth for
-- existence/size (listings walk the disk); these rows add ownership, upload
-- time and quota reporting, and power future features (sharing, admin usage
-- reports) without another disk walk.
CREATE TABLE IF NOT EXISTS files (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    relpath     TEXT NOT NULL,               -- 'folder/sub/name.ext' under the user root
    size        INTEGER NOT NULL DEFAULT 0,
    uploaded_at INTEGER NOT NULL,
    UNIQUE(owner_id, relpath)
);
CREATE INDEX IF NOT EXISTS idx_files_owner ON files(owner_id);

CREATE TABLE IF NOT EXISTS audit (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       INTEGER NOT NULL,
    username TEXT NOT NULL DEFAULT '',
    action   TEXT NOT NULL,
    detail   TEXT NOT NULL DEFAULT '',
    ip       TEXT NOT NULL DEFAULT ''
);
"""


def _connect():
    os.makedirs(os.path.dirname(DB_PATH) or '.', exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=30000')
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def get_db():
    conn = getattr(_local, 'conn', None)
    if conn is None:
        conn = _local.conn = _connect()
    return conn


def query(sql, args=(), one=False):
    cur = get_db().execute(sql, args)
    rows = cur.fetchall()
    cur.close()
    return (rows[0] if rows else None) if one else rows


def execute(sql, args=()):
    db = get_db()
    cur = db.execute(sql, args)
    db.commit()
    lastrow = cur.lastrowid
    cur.close()
    return lastrow


def audit(username, action, detail='', ip=''):
    execute('INSERT INTO audit (ts, username, action, detail, ip) VALUES (?,?,?,?,?)',
            (int(time.time()), username or '-', action, detail, ip or ''))


def init_db():
    """Create the schema and seed the bootstrap admin + optional demo data.

    Idempotent: safe to run on every worker start."""
    conn = _connect()
    conn.executescript(SCHEMA)
    conn.commit()

    # migration for databases created before per-user file spaces:
    # NULL quota_mb means "use the CCP_USER_QUOTA_MB default".
    cols = [r['name'] for r in conn.execute('PRAGMA table_info(users)')]
    if 'quota_mb' not in cols:
        conn.execute('ALTER TABLE users ADD COLUMN quota_mb INTEGER')
        conn.commit()

    # M1 — node lifecycle (see docs/ccp/db-migration-plan.md). Legacy ssh rows
    # become 'unverified' (CCP cannot know their credentials work; Verify or
    # re-onboarding promotes them); local rows run via subprocess and need no
    # credentials, so they are managed by definition.
    ncols = [r['name'] for r in conn.execute('PRAGMA table_info(nodes)')]
    if 'state' not in ncols:
        conn.execute("ALTER TABLE nodes ADD COLUMN state TEXT NOT NULL DEFAULT 'unverified'")
        conn.execute("ALTER TABLE nodes ADD COLUMN state_detail TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE nodes ADD COLUMN mac TEXT NOT NULL DEFAULT ''")
        conn.execute('ALTER TABLE nodes ADD COLUMN onboarded_at INTEGER')
        conn.execute("UPDATE nodes SET state='managed' WHERE conn='local'")
        conn.commit()

    # A worker restart aborts any in-flight onboarding thread; reset those
    # rows to a retryable state (mirrors the running-jobs reaper below).
    conn.execute("UPDATE nodes SET state='failed', "
                 "state_detail='onboarding interrupted by restart — retry' "
                 "WHERE state='onboarding'")
    conn.commit()

    # The single worker runs jobs in in-process threads; if it restarts, those
    # threads are gone, so any job still marked 'running' is orphaned. Reap them
    # so they don't linger forever.
    conn.execute("UPDATE jobs SET status='failed', finished_at=? "
                 "WHERE status='running'", (int(time.time()),))
    conn.commit()

    admin_user = os.environ.get('CCP_ADMIN_USER', 'admin')
    admin_pw = os.environ.get('CCP_ADMIN_PASSWORD', '')
    have_users = conn.execute('SELECT COUNT(*) AS c FROM users').fetchone()['c']
    if have_users == 0 and admin_pw:
        conn.execute(
            'INSERT INTO users (username, password_hash, role, created_at) VALUES (?,?,?,?)',
            (admin_user, generate_password_hash(admin_pw), 'admin', int(time.time())))
        conn.commit()
    elif have_users == 0 and not admin_pw:
        # No users and no bootstrap password → nobody can log in and there is no
        # way to create the first account. Make the cause obvious in the logs.
        sys.stderr.write(
            '[ccp] WARNING: no users exist and CCP_ADMIN_PASSWORD is not set — '
            'login is impossible. Set CCP_ADMIN_PASSWORD in .env and recreate the '
            'container (docker compose up -d --force-recreate ccp).\n')
        sys.stderr.flush()

    if os.environ.get('CCP_DEMO', '').lower() in ('1', 'true', 'yes'):
        _seed_demo(conn)

    conn.close()


def _seed_demo(conn):
    """Add a self-contained localhost node + sample script/playbook so the panel
    is immediately usable without any real SSH hosts. Only runs when the tables
    are empty."""
    now = int(time.time())
    if conn.execute('SELECT COUNT(*) AS c FROM nodes').fetchone()['c'] == 0:
        conn.execute(
            'INSERT INTO nodes (name, address, conn, ssh_user, ssh_port, groups, created_at, state) '
            'VALUES (?,?,?,?,?,?,?,?)',
            ('control-plane', 'localhost', 'local', 'root', 22, 'demo,control', now, 'managed'))
    if conn.execute('SELECT COUNT(*) AS c FROM scripts').fetchone()['c'] == 0:
        conn.execute(
            'INSERT INTO scripts (name, kind, description, content, updated_at, updated_by) '
            'VALUES (?,?,?,?,?,?)',
            ('collect-facts', 'shell', 'Print host, kernel, uptime and memory',
             'echo "== $(hostname) =="\nuname -a\nuptime\nfree -h 2>/dev/null || vm_stat',
             now, 'system'))
        conn.execute(
            'INSERT INTO scripts (name, kind, description, content, updated_at, updated_by) '
            'VALUES (?,?,?,?,?,?)',
            ('ping-check', 'playbook', 'Ansible ping + gather a couple of facts',
             '---\n'
             '- name: Connectivity and basic facts\n'
             '  hosts: all\n'
             '  gather_facts: true\n'
             '  tasks:\n'
             '    - name: Ping\n'
             '      ansible.builtin.ping:\n'
             '    - name: Show distribution\n'
             '      ansible.builtin.debug:\n'
             '        msg: "{{ ansible_distribution | default(\'unknown\') }} '
             '{{ ansible_distribution_version | default(\'\') }}"\n',
             now, 'system'))
    conn.commit()
