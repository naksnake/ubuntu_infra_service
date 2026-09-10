# API Design

Conventions unchanged: session auth + CSRF header, JSON bodies, RBAC via
`@require` (mutations = `operator`, admin ops = `admin`), every mutation
audited. New endpoints below; existing endpoints keep their contracts unless
listed under **Changed**.

## Changed

### `POST /api/nodes` (operator) — now *onboarding*, not insert

```json
{ "address": "192.168.100.21", "username": "ubuntu", "password": "…",
  "name": "rack0_sled1_gpu",        // optional — taken from the node if omitted
  "ssh_port": 22, "mac": "aa:bb:…", // optional (mac set by discovery import)
  "conn": "ssh" }                   // "local" keeps the old immediate-insert path
```

→ `201 {"id": 7, "job_id": 42}`. The node row is created in state
`onboarding`; job 42 performs credential validation → key bootstrap →
execution check and flips the state to `managed` or `failed`
(`state_detail` carries the reason). Passwords are not persisted anywhere.

Errors: `400` invalid fields (same allowlists as before; username/password
required for `conn='ssh'`), `409` duplicate name/address.

### `POST /api/run/shell`, `POST /api/run/ansible` (operator)

- Accept `cluster_id` as a third targeting option next to `node_ids` and
  `group`.
- Silently exclude non-`managed` SSH nodes; `400` if nothing eligible
  remains, with the offending states named.
- `POST /api/run/ansible` accepts either inline `playbook` (unchanged) or
  `{"playbook_path": "site.yml", "source": "/data/ccp/ansible/slurm"}`
  referencing a scanned filesystem source.

## New — node lifecycle

| Endpoint | Role | Purpose |
|---|---|---|
| `POST /api/nodes/<id>/onboard` `{username,password}` | operator | (re)run onboarding for `discovered`/`failed`/`unverified` nodes |
| `POST /api/nodes/<id>/verify` | operator | key-only execution check; promotes `unverified`→`managed` (legacy rows) |
| `POST /api/nodes/<id>/hwscan` | operator | queue a hardware rescan job |
| `POST /api/nodes/<id>/hostname` `{hostname}` | operator | hostname job: `hostnamectl set-hostname` + `/etc/hostname` + `/etc/hosts`; on success updates name + rack/sled/role |
| `GET  /api/nodes` | viewer | full inventory JSON: state, topology, hardware summary, cluster |

All four mutations return `{"job_id": N}`; progress is the normal job log.

## New — discovery

| Endpoint | Role | Purpose |
|---|---|---|
| `GET /api/discovery` | viewer | parsed DHCP leases: `[{ip, mac, hostname, expires, expired, node_id|null}]` — `node_id` set when MAC or IP already matches inventory |
| `POST /api/discovery/import` | operator | `{"systems":[{"ip","mac","hostname"}], "username","password", "onboard":true}` → creates nodes (state `discovered`) and, when `onboard` and credentials given, queues one onboarding job per node. Returns per-system `{node_id, job_id}` |

## New — clusters

| Endpoint | Role | Purpose |
|---|---|---|
| `GET  /api/clusters` | viewer | clusters with member/managed counts |
| `POST /api/clusters` `{name, description}` | operator | create (a `kind` in the body is accepted and ignored — every cluster is an execution target) |
| `DELETE /api/clusters/<id>` | operator | delete; members' `cluster_id` cleared |
| `POST /api/clusters/<id>/nodes` `{node_ids:[…]}` | operator | assign members (moves them from any previous cluster) |
| `DELETE /api/clusters/<id>/nodes/<node_id>` | operator | unassign |

## Jobs

| Endpoint | Role | Purpose |
|---|---|---|
| `GET /api/jobs/<id>/log` | viewer | raw job log as a text attachment (the job page offers Copy log / Download log) |
| `DELETE /api/jobs/<id>` | admin | delete one job **and its log file**; `409` while it is running |
| `GET /api/jobs/stats` | viewer | `{total, running, success, failed, log_files, log_bytes, oldest_at, retention_days, retention_keep}` |
| `POST /api/jobs/cleanup` `{status?, older_than_days?, keep_last?, kinds?, orphans?, dry_run?}` | admin | bulk history clean-up: `status` ∈ finished (default) / failed / success; `older_than_days` 0 = any age; `keep_last` newest N survive; `kinds` list; `orphans` (default true) also removes log files without a job row; `dry_run` only reports → `{deleted, orphans_removed, bytes_freed, dry_run, stats}`. Running jobs are never touched. `CCP_JOB_RETENTION_DAYS` / `CCP_JOB_RETENTION_KEEP` do the same automatically whenever a job starts |

## Removed — Slurm (2026-09-10)

`/api/clusters/<id>/slurm/*` (generate, deploy, action, auto) and
`PATCH /api/clusters/<id>` were removed together with the Slurm builder;
membership endpoints return `{ok}` again. Last commit with them: `f381985`.

## New — Ansible sources

| Endpoint | Role | Purpose |
|---|---|---|
| `GET /api/ansible/sources` | viewer | scan `CCP_ANSIBLE_DIRS`: `[{source, playbooks:[…], roles:[…], inventories:[…]}]` |

Path safety: `source` must be one of the configured dirs verbatim;
`playbook_path` is a validated relative path resolved and containment-checked
under it (same layered defense as the files API).

## Job kinds

`jobs.kind` values after the redesign: `shell`, `ansible`, `onboard`,
`hwscan`, `hostname`, `filedeploy`. The generic job
endpoints (`GET/DELETE /api/jobs/<id>`) are unchanged and cover all kinds.
