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
use without another schema change.

## M3 — topology (Phase 4)

```sql
ALTER TABLE nodes ADD COLUMN rack INTEGER;   -- NULL = not rack-addressed
ALTER TABLE nodes ADD COLUMN sled INTEGER;
ALTER TABLE nodes ADD COLUMN role TEXT NOT NULL DEFAULT '';
```

Backfill: parse every existing `nodes.name` once with `topology.parse()`.
Columns are recomputed whenever `name` changes (add/onboard/hostname job).

## M4–M6 — retired (clusters, Slurm settings)

M4 added the `clusters` table and `nodes.cluster_id`; M5 the Slurm
auto-deployment settings; M6 normalised cluster kinds. The Clusters feature
(and the Slurm builder before it) were removed on 2026-09-10, so none of them
is applied any more. Databases that received them keep the table and columns —
SQLite cannot drop them cheaply and nothing reads them.

## Deprecations (no DDL)

- `scripts.kind='playbook'`: rows are kept and remain runnable; the API stops
  accepting new/updated playbook-kind scripts once filesystem sources land.
- `nodes.groups` is the grouping mechanism (comma-separated tags).

## Data-safety rules

- No table is dropped or rewritten; every migration is `ADD COLUMN` or
  `CREATE TABLE IF NOT EXISTS`.
- Passwords are never stored — no schema location exists for them by design.
- `jobs.spec` for the new kinds contains addresses/usernames only; the
  executor strips secrets before insert.
- Back up `./data/ccp/ccp.db` before upgrade as usual (`sqlite3 ... .backup`
  or file copy while the container is stopped); WAL means a hot copy must
  include `-wal`/`-shm`.
