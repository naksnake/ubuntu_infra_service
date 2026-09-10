# RFC-0001: CCP — from Ansible Web UI to AI/HPC Cluster Lifecycle Platform

Status: **Accepted** · Author: CCP maintainers · Date: 2026-09-08

> **Amendment 2026-09-10:** the Slurm builder and lifecycle (Phases 6–7)
> were removed at the operator's request after repeated deployment failures
> in the reference lab; clusters remain as execution targets. Last commit
> with the feature: `f381985`. A new Slurm design will come as a new request.

## Summary

Transform the Cluster Control Panel (`lab_ccp`) from a node list with a
ClusterShell/Ansible runner into a lifecycle-management platform for AI/HPC
labs: **PXE + Discovery + Inventory + Cluster + Slurm + Monitoring**.

This RFC records the Phase 0 architecture review: what exists today, what
stays, what is redesigned, what is removed, and the migration impact. The
companion documents in this directory cover the target architecture
(`architecture.md`), database migration plan (`db-migration-plan.md`), API
design (`api-design.md`), UI redesign (`ui-redesign.md`), implementation plan
(`implementation-plan.md`) and test plan (`test-plan.md`).

## 1. Current architecture (as reviewed)

### 1.1 Process model

- One Flask app (`services/ccp/app.py`, ~780 lines) served by gunicorn with
  **1 worker × 8 gthread threads** (`services/ccp/Dockerfile`). The single
  worker is deliberate: jobs run as **in-process daemon threads**
  (`executor.start_job`) that share state with the API.
- Persistence is **SQLite in WAL mode** (`services/ccp/db.py`), one connection
  per thread, at `/data/ccp/ccp.db` on a host bind mount (`./data/ccp`).
- Job output streams to per-job log files under `/data/ccp/jobs/<id>.log`;
  the `jobs` table row (status/exit_code) is the source of truth the UI polls.
- A hard `CCP_JOB_TIMEOUT` wall-clock cap (default 900 s) bounds every job.

### 1.2 Database schema (current)

| Table     | Purpose                                                            |
|-----------|--------------------------------------------------------------------|
| `users`   | login accounts: username, password_hash, role, quota_mb            |
| `nodes`   | inventory: name, address, conn (`ssh`\|`local`), ssh_user, ssh_port, groups (CSV) |
| `scripts` | saved shell snippets **and** in-DB playbooks (kind `shell`\|`playbook`) |
| `jobs`    | job history: kind (`shell`\|`ansible`), target, spec JSON, status, exit_code |
| `files`   | per-user file space metadata (filesystem is source of truth)        |
| `audit`   | append-only action log: ts, username, action, detail, ip            |

Migrations are additive-in-place: `init_db()` runs `CREATE TABLE IF NOT
EXISTS` plus targeted `ALTER TABLE ADD COLUMN` checks (see the `quota_mb`
migration) on every worker start.

### 1.3 Execution paths

- **Shell**: `conn='local'` nodes run via `subprocess`; `conn='ssh'` nodes run
  in parallel through ClusterShell (`task_self()`), bucketed per
  (ssh_user, ssh_port). If `/data/ccp/ssh/id_ccp` exists it is passed as `-i`;
  **nothing generates or deploys that key**.
- **Ansible**: playbook text (from the UI or the scripts table) is written to
  a temp file with a generated INI inventory and executed with
  `ansible-playbook` under a kill-timer watchdog.
- Both paths re-validate node fields against allowlist regexes before
  execution because the SQLite file is host-writable (defense in depth).

### 1.4 Security foundations (working well)

Session login + CSRF, role ladder `viewer < operator < admin`, per-IP login
lockout, append-only audit log with client IP, security headers, per-user
file space with a layered traversal defense, strict input allowlists on every
field that reaches a NodeSet or inventory file.

### 1.5 UI

Server-rendered Jinja + ~75 lines of vanilla JS. Navigation: Dashboard,
Nodes, ClusterShell, Ansible, Scripts, Jobs, Files, (admin) Users, Audit.

### 1.6 Adjacent services CCP can build on

- `lab_dhcp` (dnsmasq) writes leases to `./data/dnsmasq.leases` on the host;
  `lab_monitor` already parses it (v4 + v6 lines). **CCP does not mount it.**
- `lab_ipxe_manager` handles PXE boot menus and autoinstall — out of CCP scope.

### 1.7 Defects found during review

1. **P0 — nodes exist without usable credentials.** `POST /api/nodes` is a
   bare INSERT. No credential capture, no reachability check, no key
   deployment, no execution check. Every SSH job against such a node fails
   with `Permission denied`; the inventory silently fills with dead rows.
2. **docker-compose healthcheck bug**: the `ccp` service healthcheck probes
   `http://127.0.0.1:8090/` (the monitor's port) instead of
   `http://127.0.0.1:8060/healthz`, so compose reports `ccp` unhealthy
   forever. The Dockerfile healthcheck is correct; compose overrides it.
3. **No discovery**: DHCP lease knowledge lives only in the monitor; nodes
   are typed in by hand (name, address, user, port, plus free-text groups).
4. **In-DB playbook editing** (`scripts.kind='playbook'`) conflicts with the
   project vision ("CCP is not a development environment").

## 2. What stays (platform foundations)

These capabilities are preserved as-is and reused by every new feature:

- **Authentication** (session login, lockout) — unchanged.
- **RBAC** (`viewer`/`operator`/`admin`, `@require`) — unchanged; new
  endpoints slot into the existing ladder (mutations = operator, admin pages
  = admin).
- **Audit logging** — unchanged; every new action calls `log_action`.
- **Job history** — unchanged mechanism; new job kinds (`onboard`, `hwscan`,
  `hostname`, `filedeploy`) reuse the same table, log files, timeout watchdog and
  the Jobs UI.
- **ClusterShell execution** — unchanged engine; gains a lifecycle gate
  (only `managed` nodes are eligible targets).
- **Ansible execution** — unchanged engine; gains local-filesystem playbook
  sources.
- **Per-user Files**, SQLite/WAL, single-worker + job-thread process model,
  input allowlists, additive in-place migration style.

## 3. What is redesigned

| Area | Today | Target |
|------|-------|--------|
| Node onboarding | bare INSERT | lifecycle: Discover → Credential Validation → SSH Bootstrap → **Managed** (see §4) |
| Node schema | name/address/conn/ssh | + `state`, `state_detail`, `mac`, `rack`/`sled`/`role` (derived), `cluster_id`, `onboarded_at` |
| Inventory source | manual form | **DHCP lease discovery first**; manual add becomes the fallback |
| Topology | free-text `groups` CSV | hostname-driven: `rack0_sled1_gpu` → rack 0, sled 1, role `gpu`; groups kept for ad-hoc tagging |
| Hostname | not managed | one-click rename: `hostnamectl set-hostname` + `/etc/hostname` + `/etc/hosts`, inventory refreshed immediately |
| Hardware | not modeled | `hardware` table (CPU/mem/storage/net/GPU/OS + raw JSON), auto-collected after onboarding |
| Clusters | none | first-class `clusters` table; a cluster is an execution target |
| Ansible sources | textarea / in-DB scripts | local filesystem paths (`CCP_ANSIBLE_DIRS`), scanned for playbooks/roles/inventories; development stays external |
| Navigation | flat 7 items | Dashboard · Discovery · Nodes · Clusters · Rack View · ClusterShell · Ansible · Jobs · Files · Deploy files · Admin |
| Dashboard | 4 counters | lifecycle funnel (discovered/onboarding/managed/failed), cluster health, recent jobs |

## 4. Node lifecycle (the P0 fix)

```
discovered ──onboard──▶ onboarding ──✓──▶ managed
    ▲                       │ ✗
 DHCP import / manual       ▼
                          failed ──retry onboard──▶ onboarding
legacy rows ─▶ unverified ──verify (key works) / onboard──▶ managed
```

A node is **managed** only after, in one atomic onboarding job:

1. password authentication succeeds (`sshpass` + ssh),
2. the CCP public key is installed into `authorized_keys`,
3. a command executes successfully over **key** auth.

Passwords are held in process memory for the duration of the job only — never
written to `jobs.spec`, the database, or job logs. CCP generates its ed25519
keypair at startup if `/data/ccp/ssh/id_ccp` is missing.

**Only `managed` (or `conn='local'`) nodes can be targeted by jobs.** The
gate is enforced at selection time and re-checked in the executor.

## 5. What is removed / deprecated

- **Playbook editing in the Scripts page**: creating/updating
  `kind='playbook'` scripts is deprecated. Existing playbook scripts remain
  runnable (no data loss) but the UI steers to filesystem sources. Shell
  snippets stay — they are operational tooling, not development.
- **Manual-first node creation**: the form survives as "Add node manually"
  inside Discovery/Nodes but always goes through the onboarding pipeline.
  There is no path that creates an SSH node in `managed` state without the
  three checks.
- **Rack/location as manual input**: never introduced; topology is derived
  from hostnames only.
- Explicitly **not built** (anti-goals): playbook/role editors, AWX-style
  workflow designers, survey forms, per-job credential prompts.

## 6. Migration impact

- **Schema**: additive only — `ALTER TABLE nodes ADD COLUMN …`, two new
  tables (`hardware`, `clusters`). Applied automatically by `init_db()` on
  first start after upgrade, same pattern as the existing `quota_mb`
  migration. SQLite file is never rewritten; rollback = run the previous
  image (old code ignores new columns/tables).
- **Existing node rows**: `conn='local'` rows become `managed` (they execute
  via subprocess, no credentials involved). `conn='ssh'` rows become
  `unverified`: they keep working *only after* a one-click **Verify** (key
  auth already works) or a re-onboard (password) promotes them to `managed`.
  This is the honest reading of the P0 requirement — CCP cannot know whether
  a legacy row is reachable, so it must not claim it is managed.
- **API**: `POST /api/nodes` changes semantics (returns a node in
  `onboarding` state + a job id instead of a ready row). This is a breaking
  change for API users; acceptable per project charter ("backward
  compatibility preferred but not mandatory"). All other existing endpoints
  keep their contracts.
- **compose**: `ccp` gains a read-only mount of `./data/dnsmasq.leases` and
  the healthcheck port fix; both are safe to apply with
  `docker compose up -d ccp`.
- **Jobs history**: untouched; new kinds only add rows.

## 7. Rollout

Small iterative commits, one phase per commit (see
`implementation-plan.md`): docs → node lifecycle (P0) → DHCP discovery →
hardware discovery → hostname topology + rack view → clusters → Ansible
filesystem sources → file deployment. (Slurm builder/lifecycle: removed, see
amendment.) Each commit leaves the
panel deployable and the test suite green.
