# Implementation Plan

One phase per commit (or a few small commits per phase); every commit leaves
the stack deployable and `services/ccp` tests green. Priorities follow the
project charter: P0 first.

| # | Phase | Contents | Priority |
|---|-------|----------|----------|
| 0 | RFC + design docs | this directory | — |
| 1 | **Node lifecycle onboarding** (P0 fix) | M1 migration; `ensure_ssh_key()`; `onboard` job kind (password check → key bootstrap → key-auth execution check); secrets kept out of DB/logs; targeting gate (`managed`/`local` only); `POST /api/nodes` semantics change + `/onboard`, `/verify`; Nodes UI with state badges + credential modal; compose healthcheck port fix | P0 |
| 2 | **DHCP discovery** | `discovery.py` lease parser; ro mount of `data/dnsmasq.leases` into ccp (compose); Discovery page + `GET /api/discovery`, `POST /api/discovery/import` (bulk import & onboard); dashboard callout | P0 |
| 3 | **Hardware discovery** | M2 migration; fact script + parser; `hwscan` job kind, auto-chained after onboarding; node detail drawer showing facts | P0 |
| 4 | **Hostname topology + rename + Rack View** | M3 migration; `topology.py` parser + backfill; `hostname` job kind (`hostnamectl` + `/etc/hostname` + `/etc/hosts`, `sudo -n` fallback); Rack View page | P0 |
| 5 | **Cluster objects** | M4 migration; clusters CRUD + membership; `cluster_id` targeting in run APIs and the shared node selector; Clusters page; dashboard cluster cards | P1 |
| 6 | **Ansible filesystem sources** | `CCP_ANSIBLE_DIRS` env + compose mount example; source scanner + containment checks; `playbook_path` in run API; Ansible page redesign; deprecate playbook-kind script writes | P1 |
| 7 | **Slurm builder** | `slurm.py` conf/gres generation from hardware; generate/preview/deploy endpoints; built-in deployment playbook (Ubuntu/Debian `slurm-wlm`, munge key distribution, config push, services) | P2 |
| 8 | **Slurm lifecycle** | `slurm_state` machine + stage actions (discover/validate/benchmark/monitor/cleanup) as jobs; lifecycle strip UI; node-to-node benchmark via `srun` (not loopback) | P2 |

Deferred (documented, not in this iteration): monitoring dashboards &
capacity views (P3), benchmark framework beyond the Slurm-stage basics (P3),
reports (P3), node "offline" liveness tracking, dnsmasq event push (polling
is fine at lab scale).

## Risk register

| Risk | Mitigation |
|---|---|
| sshpass/ssh edge cases (host key churn after reinstall) | `StrictHostKeyChecking=no` + `UserKnownHostsFile=/dev/null` already the project stance for a PXE lab |
| password leakage | secrets never enter `jobs.spec`/logs; passed via env (`SSHPASS`), deleted from memory at thread start; code-reviewed test asserts DB/log cleanliness |
| legacy nodes breaking | they keep existing; only *execution eligibility* is gated, with one-click Verify to restore it |
| Slurm deploy variance across distros | default to Ubuntu/Debian `slurm-wlm` packages with a version gate (versions listed per node, pin what every node offers); when members run different Ubuntu releases no common package exists, so the deploy offers **build from source** — the same SchedMD release (default 25.11.8, optional local-mirror tarball URL) compiled on every node; playbook is visible/auditable in the job log |
| single-worker job loss on restart | existing orphan reaper marks running jobs failed; onboarding is retryable |
