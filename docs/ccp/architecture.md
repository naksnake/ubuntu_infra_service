# CCP Target Architecture

Companion to [RFC-0001](RFC-0001-lifecycle-platform.md).

## Design tenets

1. **Preserve the foundations**: auth, RBAC, audit, job history, ClusterShell
   and Ansible execution are reused, not rebuilt.
2. **Keep the process model**: one Flask worker, in-process job threads,
   SQLite/WAL, per-job log files. It comfortably serves a lab-scale control
   plane (tens to a few hundred nodes) and keeps the deployment a single
   container with one bind mount.
3. **Everything long-running is a job**: onboarding, hardware scans, hostname
   changes and file deployments all go through `executor.start_job`, so they
   inherit history, logs, audit, timeouts and the Jobs UI for free.
4. **Discovery first, derive don't ask**: DHCP leases feed the inventory;
   rack/sled/role derive from hostnames. Operators type usernames, passwords
   and hostnames — nothing else.
5. **CCP is not an IDE**: playbooks and roles are developed outside CCP and
   consumed from local filesystem paths.

## Component view

```
┌────────────────────────── lab_ccp container ──────────────────────────┐
│  Flask app (gunicorn, 1 worker × N threads)                           │
│                                                                       │
│  auth / RBAC / CSRF / audit          pages + JSON API                 │
│        │                                   │                          │
│  ┌─────┴─────────┐   ┌────────────────────┴──────────────────┐        │
│  │ discovery.py  │   │ lifecycle / topology / files           │        │
│  │ lease parser  │   │ (app.py routes + helpers)              │        │
│  └─────┬─────────┘   └───────────────┬────────────────────────┘        │
│        │                             │ start_job(kind, spec, secret)   │
│        │                     ┌───────▼────────┐                        │
│        │                     │  executor.py   │  job threads           │
│        │                     │  shell│ansible │──▶ /data/ccp/jobs/*.log│
│        │                     │  onboard│hwscan│                        │
│        │                     │  hostname│files│                        │
│        │                     └───────┬────────┘                        │
│        │                             │ ssh / sshpass / clush /         │
│        │                             │ ansible-playbook                │
│  ┌─────▼─────────────────────────────▼─────┐                           │
│  │ SQLite /data/ccp/ccp.db  (WAL)          │                           │
│  │ users nodes hardware scripts            │                           │
│  │ jobs files audit                        │                           │
│  └─────────────────────────────────────────┘                           │
└───────────────────────────────────────────────────────────────────────┘
     ▲ ro mount                                   ▲ rw mount
 ./data/dnsmasq.leases (written by lab_dhcp)   ./data/ccp (db, jobs, ssh key,
                                               files, ansible sources)
```

New modules keep `app.py` from growing unbounded:

- `discovery.py` — dnsmasq lease parsing (v4 + v6 lines, same format the
  monitor parses) and cross-referencing against the nodes table by MAC/IP.
- `topology.py` — hostname → (rack, sled, role) parsing and validation.
- `executor.py` — gains job kinds `onboard`, `hwscan`, `hostname`,
  `filedeploy`, plus `ensure_ssh_key()` (ed25519 keypair generated at
  startup when missing).

## Node lifecycle engine

States: `discovered` → `onboarding` → `managed`, with `failed` (retryable)
and `unverified` (legacy rows; promoted by Verify or re-onboard).
`conn='local'` nodes are always `managed`.

The onboarding job (kind `onboard`) is the only writer that can set
`state='managed'` on an SSH node, and it does so only after all three checks
pass in order:

| Step | Mechanism | Failure state_detail |
|------|-----------|----------------------|
| 1. credential validation | `sshpass -e ssh <user>@<addr> true` | `auth failed` / `unreachable` |
| 2. key bootstrap | append CCP pubkey to `~/.ssh/authorized_keys` (idempotent, mkdir+chmod) via password auth | `bootstrap failed` |
| 3. execution check | `ssh -i id_ccp -o BatchMode=yes <user>@<addr> hostname` | `key auth failed` |

The remote hostname reported by step 3 refreshes `nodes.name` (if the node
was imported nameless) and the topology columns. On success the executor
chains a `hwscan` job automatically.

**Secrets path**: passwords travel `request JSON → in-memory dict keyed by
job id → job thread → sshpass via environment`, and are deleted from the
dict when the job thread starts. They never touch `jobs.spec`, the DB, or
logs. A worker restart during onboarding leaves the job `failed` (existing
orphan-reaping already handles this) — the operator retries.

## Targeting gate

`_selected_nodes` (shell/ansible launch) and `_resolve_nodes` (executor)
both filter to `state='managed' OR conn='local'`. Targets are node ids or a
group name (`nodes.groups`), expanded to eligible members.

## Hardware discovery

`hwscan` runs a POSIX-sh fact script over SSH (single `ssh` subprocess per
node — no ClusterShell needed for a per-node job) that emits `KEY=VALUE`
sections for OS, kernel, CPU (model/sockets/cores/threads), memory, block
devices, NICs, GPUs (`nvidia-smi` first, `lspci` fallback) and optional
InfiniBand. The executor parses it into the `hardware` row (summary columns +
`raw_json`) — the input to the node detail view and the rack view.

## Hostname-driven topology

`rack(\d+)[_-]sled(\d+)[_-](role)` parsed from `nodes.name` at write time
into `rack`, `sled`, `role` columns (NULL when the name doesn't match — the
node still works, it just doesn't place in the rack view). The `hostname`
job applies `hostnamectl set-hostname` (falling back to `/etc/hostname` +
`hostname`) and rewrites `/etc/hosts`, using `sudo -n` when the SSH user is
not root; on success it updates `nodes.name` + topology columns in the same
transaction, so the inventory reflects the change immediately.

## Removed: Clusters (2026-09-10)

The first-class `clusters` table, membership (`nodes.cluster_id`), the
Clusters page, the cluster selector on the ClusterShell/Ansible/Deploy-files
pages and the dashboard cluster cards were removed at the operator's request
together with the Slurm builder. Grouping is done with `nodes.groups` (comma
separated tags) as before. Existing databases keep the unused table and
column; nothing reads them.

## Removed: Slurm builder and lifecycle (2026-09-10)

Phases 6–7 (slurm.conf/gres.conf generation from hardware, the built-in
deployment playbook with version gates and source builds, the
INIT→…→CLEANUP lifecycle actions, the automatic deployment pipeline and its
Clusters-page controls) were removed at the operator's request after repeated
deployment failures in the reference lab. The last commit that contains the
complete feature, with its tests, is `f381985`; everything generic that the
work produced stays: per-host output framing and the ordered multi-stage
console renderer, the job-log copy/download, readable YAML Ansible results,
GPU-device awareness in hardware facts, and the hostname/`/etc/hosts` fixes.
Databases created before the removal keep their unused `slurm_*` columns.

## Ansible sources

`CCP_ANSIBLE_DIRS` (colon-separated container paths, e.g.
`/data/ccp/ansible`) are scanned read-only for `*.yml`/`*.yaml` playbooks
(top-level list with `hosts:`), with sibling `roles/`, `group_vars/`,
`host_vars/`, `inventories/` honored by running `ansible-playbook` with the
playbook's directory as cwd. CCP never edits these files.

## Sizing and limits

SQLite/WAL + threaded jobs holds to ~hundreds of nodes and low concurrent
job counts — the stated target (lab, not fleet). Known ceilings, accepted:
single host, no HA, job concurrency bounded by threads, lease file polling
(no dnsmasq DBus). Revisit only if the target changes.
