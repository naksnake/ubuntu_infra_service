# Test Plan

Style: keep the repo's existing harness — plain-python test scripts using
Flask's test client (`test_userfiles.py` pattern: env-isolated temp DB,
`check()` assertions, exit non-zero on failure). No network or real SSH in
unit tests: the executor's SSH entry points are monkeypatchable functions so
lifecycle logic is tested by stubbing them.

## Unit / functional (automated, per phase)

### Phase 1 — node lifecycle (`test_lifecycle.py`)
- `POST /api/nodes` (ssh) without username/password → 400.
- Onboard happy path (SSH stubs succeed): node passes
  `onboarding → managed`, `onboarded_at` set, audit rows written.
- Step failures: password auth fails / key install fails / key exec fails →
  `failed` + correct `state_detail`; retry via `/onboard` works.
- **Secret hygiene**: after onboarding, the password string appears nowhere
  in `jobs.spec`, the job log file, or the audit table.
- Targeting gate: run/shell with only a `failed` node → 400; mixed selection
  executes only managed/local nodes; executor `_resolve_nodes` re-filters.
- Legacy migration: pre-migration DB (row without `state`) upgrades in place;
  `conn='local'` → managed, ssh → unverified; `/verify` promotes when the
  key-check stub succeeds.
- RBAC: viewer cannot onboard/verify; anonymous gets 401.

### Phase 2 — discovery (`test_discovery.py`)
- Lease parser: v4 lines, `*` hostname, expired, infinite (0), DHCPv6 lines,
  malformed lines skipped.
- Cross-reference: lease matching an existing node by MAC (case-insensitive)
  or IP carries `node_id`.
- Import: creates `discovered` nodes with MAC; with credentials queues one
  onboard job each; duplicate import → existing node reported, not duplicated.

### Phase 3 — hardware (`test_hardware.py`)
- Fact-output parser: full fixture (GPU node), CPU-only node, missing
  sections tolerated; summary columns + raw_json populated.
- Rescan replaces the row (single row per node).

### Phase 4 — topology (`test_topology.py`)
- `parse()`: `rack0_sled1_gpu`, `rack12-sled3-cpu`, non-matching names → None
  fields; case handling; backfill sets columns on migration.
- Hostname API: invalid target names rejected (allowlist); success stub
  updates `nodes.name` + rack/sled/role immediately; failure leaves name
  untouched.

### Phase 5 — clusters (`test_clusters.py`)
- CRUD + membership move semantics; delete clears membership.
- `cluster_id` targeting expands to managed members only.

### Phase 6 — slurm (`test_slurm.py`)
- slurm.conf generation from hardware fixtures: CPUs/RealMemory/Sockets/
  ThreadsPerCore/Gres lines; controller line; partition line; nodes without
  hardware fall back to safe defaults.
- gres.conf only for gpu_count > 0.
- Lifecycle transitions: stage action on wrong state → 400; success advances
  `slurm_state`.

## Regression
- `test_userfiles.py` (existing 64 checks) must stay green every phase.

## Manual / integration checklist (per release, on the reference lab)
1. Fresh `./deploy.sh`; compose reports **ccp healthy** (healthcheck fix).
2. PXE-boot a node → appears in Discovery with IP/MAC/hostname.
3. Import & onboard with username/password → state walks
   onboarding → managed; `~/.ssh/authorized_keys` on the node contains the
   CCP key exactly once (idempotent on re-onboard).
4. Wrong password → failed + "auth failed"; retry with correct password.
5. Hardware facts visible after onboarding (CPU/mem/GPU/OS correct).
6. Rename node to `rack0_sled1_gpu` → `hostnamectl status` on the node,
   `/etc/hosts` updated, Rack View places it, inventory shows new name
   without reload tricks.
7. ClusterShell + Ansible run against a cluster target; non-managed node is
   not selectable.
8. Slurm cluster on ≥2 nodes: generate (preview sane), deploy, `sinfo` all
   nodes idle, `srun -N2 hostname` returns both hostnames (node-to-node, not
   loopback), cleanup removes services.
9. Job history/audit shows every step with the acting user; no password
   anywhere in `/data/ccp`.

## How to run

```bash
cd services/ccp
python3 test_userfiles.py && python3 test_lifecycle.py && \
python3 test_discovery.py && python3 test_hardware.py && \
python3 test_topology.py && python3 test_clusters.py && python3 test_slurm.py
```

(Tests require only Flask; executor SSH calls are stubbed.)
