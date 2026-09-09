# Database Migration Plan

Mechanism: the existing additive in-place style — `init_db()` runs
`CREATE TABLE IF NOT EXISTS` for new tables and checks
`PRAGMA table_info(...)` before each `ALTER TABLE ... ADD COLUMN` (the
pattern already used for `users.quota_mb`). Idempotent, runs on every worker
start, no external migration tool. Rollback = deploy the previous image;
old code ignores the new columns/tables.

## M1 — node lifecycle (Phase 1)

```sql
ALTER TABLE nodes ADD COLUMN state        TEXT NOT NULL DEFAULT 'unverified';
      -- discovered | onboarding | managed | failed | unverified
ALTER TABLE nodes ADD COLUMN state_detail TEXT NOT NULL DEFAULT '';
ALTER TABLE nodes ADD COLUMN mac          TEXT NOT NULL DEFAULT '';
ALTER TABLE nodes ADD COLUMN onboarded_at INTEGER;          -- NULL until managed
```

Backfill (one-time, guarded by the column-existence check):

```sql
UPDATE nodes SET state='managed' WHERE conn='local';
-- ssh rows keep the 'unverified' default: they must pass Verify or onboarding
```

`jobs.kind` gains values `onboard`, `hwscan` (TEXT column — no DDL needed).

## M2 — hardware (Phase 3)

```sql
CREATE TABLE IF NOT EXISTS hardware (
    node_id     INTEGER PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
    cpu_model   TEXT NOT NULL DEFAULT '',
    cpu_sockets INTEGER,
    cpu_cores   INTEGER,          -- total logical CPUs (Slurm CPUs=)
    threads_per_core INTEGER,
    mem_mb      INTEGER,
    disks       TEXT NOT NULL DEFAULT '',   -- summary, e.g. 'nvme0n1 1.9TB, sda 480GB'
    nics        TEXT NOT NULL DEFAULT '',   -- summary, e.g. 'eno1 192.168.100.21'
    gpu_count   INTEGER NOT NULL DEFAULT 0,
    gpu_model   TEXT NOT NULL DEFAULT '',
    os_name     TEXT NOT NULL DEFAULT '',
    kernel      TEXT NOT NULL DEFAULT '',
    infiniband  TEXT NOT NULL DEFAULT '',
    raw_json    TEXT NOT NULL DEFAULT '{}', -- full parsed fact payload
    updated_at  INTEGER NOT NULL
);
```

One row per node (PRIMARY KEY = node_id); a rescan replaces the row.
`raw_json` keeps everything the summary columns don't model, for future
cluster generation without another schema change.

## M3 — topology (Phase 4)

```sql
ALTER TABLE nodes ADD COLUMN rack INTEGER;   -- NULL = not rack-addressed
ALTER TABLE nodes ADD COLUMN sled INTEGER;
ALTER TABLE nodes ADD COLUMN role TEXT NOT NULL DEFAULT '';
```

Backfill: parse every existing `nodes.name` once with `topology.parse()`.
Columns are recomputed whenever `name` changes (add/onboard/hostname job).

## M4 — clusters (Phase 5/6/7)

```sql
CREATE TABLE IF NOT EXISTS clusters (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL UNIQUE,
    kind         TEXT NOT NULL DEFAULT 'generic',   -- generic | slurm
    description  TEXT NOT NULL DEFAULT '',
    slurm_state  TEXT NOT NULL DEFAULT 'INIT',
        -- INIT | DISCOVER | DEPLOY | VALIDATE | BENCHMARK | REPORT | MONITOR | CLEANUP
    controller_node_id INTEGER REFERENCES nodes(id) ON DELETE SET NULL,
    slurm_conf   TEXT NOT NULL DEFAULT '',
    gres_conf    TEXT NOT NULL DEFAULT '',
    created_by   TEXT NOT NULL DEFAULT '',
    created_at   INTEGER NOT NULL
);

ALTER TABLE nodes ADD COLUMN cluster_id INTEGER REFERENCES nodes(id); -- see note
```

Note: SQLite `ALTER TABLE ADD COLUMN` cannot add a foreign key that is
enforced retroactively in older versions; membership integrity is enforced in
application code (cluster delete clears `nodes.cluster_id`).

## M5 — automatic deployment settings (Clusters redesign)

Additive `ALTER TABLE clusters ADD COLUMN`, applied when `auto_deploy` is
missing:

| column | type | default | meaning |
|---|---|---|---|
| `auto_deploy` | INTEGER | 1 | re-run the pipeline when membership changes / a member finishes onboarding |
| `install_from` | TEXT | `'auto'` | `auto` (decide from the nodes' facts) / `apt` / `source` |
| `slurm_version` | TEXT | `''` | apt pin or upstream release, per `install_from` |
| `tarball_url` | TEXT | `''` | local mirror for the source tarball |

No data migration: existing Slurm clusters get auto-deploy on with automatic
install selection, which is what the one-click button uses.

## Deprecations (no DDL)

- `scripts.kind='playbook'`: rows are kept and remain runnable; the API stops
  accepting new/updated playbook-kind scripts once filesystem sources land.
- `nodes.groups` stays as ad-hoc tagging alongside clusters.

## Data-safety rules

- No table is dropped or rewritten; every migration is `ADD COLUMN` or
  `CREATE TABLE IF NOT EXISTS`.
- Passwords are never stored — no schema location exists for them by design.
- `jobs.spec` for the new kinds contains addresses/usernames only; the
  executor strips secrets before insert.
- Back up `./data/ccp/ccp.db` before upgrade as usual (`sqlite3 ... .backup`
  or file copy while the container is stopped); WAL means a hot copy must
  include `-wal`/`-shm`.
