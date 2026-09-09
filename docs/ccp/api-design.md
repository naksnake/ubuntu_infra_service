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
| `POST /api/clusters` `{name, kind, description}` | operator | create |
| `DELETE /api/clusters/<id>` | operator | delete; members' `cluster_id` cleared |
| `POST /api/clusters/<id>/nodes` `{node_ids:[…]}` | operator | assign members (moves them from any previous cluster) |
| `DELETE /api/clusters/<id>/nodes/<node_id>` | operator | unassign |

## New — Slurm

| Endpoint | Role | Purpose |
|---|---|---|
| `POST /api/clusters/<id>/slurm/generate` `{controller_node_id}` | operator | generate + store slurm.conf/gres.conf from `hardware`; returns both texts for preview |
| `POST /api/clusters/<id>/slurm/deploy` | operator | run the built-in deployment playbook via the Ansible engine → `{job_id}` |
| `POST /api/clusters/<id>/slurm/action` `{stage}` | operator | run a lifecycle stage: `discover`, `validate`, `sbatch`, `benchmark`, `report`, `monitor`, `diagnose` (collect logs, any state), `cleanup` → `{job_id}`; advances `slurm_state` on success |
| `POST /api/clusters/<id>/slurm/auto` `{reinstall?, run_tests?}` | operator | **automatic deployment**: one `slurm_auto` job — facts → hostnames → plan → generate → deploy → validate → sbatch — using the cluster's saved settings → `{job_id}`; `409` while one is running |
| `PATCH /api/clusters/<id>` `{auto_deploy?, install_from?, slurm_version?, tarball_url?, controller_node_id?, description?}` | operator | cluster settings the pipeline reads; `install_from` ∈ auto/apt/source, validated like deploy |
| `POST /api/clusters/<id>/nodes`, `DELETE /api/clusters/<id>/nodes/<nid>` | operator | now return `{ok, job_id}`: when the cluster is Slurm with `auto_deploy` on, the pipeline is re-run and its job id returned |
| `GET /api/jobs/<id>/log` | viewer | raw job log as a text attachment |

## New — Ansible sources

| Endpoint | Role | Purpose |
|---|---|---|
| `GET /api/ansible/sources` | viewer | scan `CCP_ANSIBLE_DIRS`: `[{source, playbooks:[…], roles:[…], inventories:[…]}]` |

Path safety: `source` must be one of the configured dirs verbatim;
`playbook_path` is a validated relative path resolved and containment-checked
under it (same layered defense as the files API).

## Job kinds

`jobs.kind` values after the redesign: `shell`, `ansible`, `onboard`,
`hwscan`, `hostname`, `slurm_deploy`, `slurm_action`. The generic job
endpoints (`GET/DELETE /api/jobs/<id>`) are unchanged and cover all kinds.
