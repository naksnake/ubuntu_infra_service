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
   changes and Slurm operations all go through `executor.start_job`, so they
   inherit history, logs, audit, timeouts and the Jobs UI for free.
4. **Discovery first, derive don't ask**: DHCP leases feed the inventory;
   rack/sled/role derive from hostnames; slurm.conf derives from discovered
   hardware. Operators type usernames, passwords and hostnames — nothing else.
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
│  │ discovery.py  │   │ lifecycle / topology / cluster / slurm │        │
│  │ lease parser  │   │ (app.py routes + helpers)              │        │
│  └─────┬─────────┘   └───────────────┬────────────────────────┘        │
│        │                             │ start_job(kind, spec, secret)   │
│        │                     ┌───────▼────────┐                        │
│        │                     │  executor.py   │  job threads           │
│        │                     │  shell│ansible │──▶ /data/ccp/jobs/*.log│
│        │                     │  onboard│hwscan│                        │
│        │                     │  hostname│slurm│                        │
│        │                     └───────┬────────┘                        │
│        │                             │ ssh / sshpass / clush /         │
│        │                             │ ansible-playbook                │
│  ┌─────▼─────────────────────────────▼─────┐                           │
│  │ SQLite /data/ccp/ccp.db  (WAL)          │                           │
│  │ users nodes hardware clusters scripts   │                           │
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
- `slurm.py` — slurm.conf / gres.conf generation from `hardware` rows and the
  built-in deployment playbook template.
- `executor.py` — gains job kinds `onboard`, `hwscan`, `hostname`,
  `slurm_deploy`, `slurm_action`, plus `ensure_ssh_key()` (ed25519 keypair
  generated at startup when missing).

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
both filter to `state='managed' OR conn='local'`. A cluster id is a third
selector next to node ids and group names; it expands to the cluster's
managed members.

## Hardware discovery

`hwscan` runs a POSIX-sh fact script over SSH (single `ssh` subprocess per
node — no ClusterShell needed for a per-node job) that emits `KEY=VALUE`
sections for OS, kernel, CPU (model/sockets/cores/threads), memory, block
devices, NICs, GPUs (`nvidia-smi` first, `lspci` fallback) and optional
InfiniBand. The executor parses it into the `hardware` row (summary columns +
`raw_json`) — the input to rack view and the Slurm builder.

## Hostname-driven topology

`rack(\d+)[_-]sled(\d+)[_-](role)` parsed from `nodes.name` at write time
into `rack`, `sled`, `role` columns (NULL when the name doesn't match — the
node still works, it just doesn't place in the rack view). The `hostname`
job applies `hostnamectl set-hostname` (falling back to `/etc/hostname` +
`hostname`) and rewrites `/etc/hosts`, using `sudo -n` when the SSH user is
not root; on success it updates `nodes.name` + topology columns in the same
transaction, so the inventory reflects the change immediately.

## Clusters and Slurm

`clusters` is a first-class table; `nodes.cluster_id` is the membership edge
(a node belongs to at most one cluster — matches physical reality for Slurm).
A Slurm-kind cluster carries: controller node id, generated `slurm.conf` /
`gres.conf` text, and a lifecycle state machine
`INIT → DISCOVER → DEPLOY → VALIDATE → BENCHMARK → REPORT → MONITOR ⇄ CLEANUP`
where each stage maps to jobs on the existing engine:

- DISCOVER = hwscan across members; DEPLOY = built-in Ansible playbook
  (read-only version preflight and gates first, then munge key distribution,
  Slurm install, config push, service start). Slurm comes either from the
  distro `slurm-wlm` package (optionally pinned to a version every node's apt
  sources offer) or, for a fleet whose nodes run different Ubuntu releases and
  therefore can never share a package version, from a **source build** of one
  SchedMD release on every node (default 25.11.8; tarball URL overridable for
  a local mirror). VALIDATE = `sinfo` + `srun hostname` on the controller,
  plus an **sbatch test**: a batch job shaped like an AI training run (one
  task per node, GPU inventory, timed numpy/torch step) submitted with
  `sbatch --wait`, checked with `scontrol`, output pulled from the batch host;
  BENCHMARK = node-to-node checks run *through Slurm* (`srun`), not loopback;
  REPORT = aggregation of the stored job outputs; CLEANUP = teardown playbook
  (always allowed — it is the recovery path for a configless slurmd).
- **Automatic deployment** (`slurm_auto` job) is the primary way a cluster is
  built: facts (fresh hardware + pre-flight from every member, in parallel;
  an unreachable member stops the run before anything changes) → hostnames
  (a box that does not answer to its inventory name is renamed; warn-only)
  → plan (controller: the saved one, else a node without GPUs; install:
  distro packages pinned to the newest version every node offers, else the
  same upstream release built from source — also when a node already runs a
  source build) → generate (GPUs declared only up to the device files that
  exist right now) → deploy (the built-in playbook) → validate → sbatch.
  Stages are framed with `##STAGE##` markers and the first failure stops the
  run. Membership changes and a member finishing onboarding re-run it when
  the cluster's `auto_deploy` is on; one pipeline per cluster at a time.
  The single-step actions remain for operators who want them.
- Pre-flight also verifies, on every node declared with GPUs, that the
  `File=` device files in gres.conf exist *now* (after an `nvidia-smi -L`
  warm-up that recreates them when the driver is installed): slurmd waits
  20 s for a missing device and then exits, which systemd reports only as
  "control process exited". A missing device fails the deploy before anything
  is changed, naming the node, the files and the fix; nvidia-persistenced is
  enabled where present so the files survive reboots. The deploy writes a
  cgroup.conf with `CgroupPlugin=disabled` to match the generated
  linuxproc/none configuration, so slurmd never depends on the cgroup/v2
  re-parenting that only happens when systemd launches it.
- Failure must explain itself. systemd only ever reports "control process
  exited with error code", so both daemon starts in the deploy playbook are
  block/rescue: on failure the rescue prints `systemctl status`, the journal,
  the effective unit with drop-ins and an 8-second foreground run of the
  daemon (its own fatal on stdout), then fails the host. Independently, the
  **Collect logs** action (any state, changes nothing) gathers the same bundle
  plus every config file, ports, hosts and GPUs from every member into one job
  log, and the job page offers Copy log / Download log so an operator can hand
  it to whoever is helping. Ansible results print as YAML so multi-line
  messages stay readable.

Config generation is pure-python from `hardware` rows (`slurm.py`) so it is
unit-testable without any node.

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
