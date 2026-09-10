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
| 5 | ~~Cluster objects~~ | removed 2026-09-10 with the Slurm builder; `nodes.groups` remains the grouping/targeting mechanism | — |
| 6 | **Ansible filesystem sources** | `CCP_ANSIBLE_DIRS` env + compose mount example; source scanner + containment checks; `playbook_path` in run API; Ansible page redesign; deprecate playbook-kind script writes | P1 |
| 7 | ~~Slurm builder~~ | removed 2026-09-10 at the operator's request after repeated lab deployment failures (last commit with it: `f381985`) | — |
| 8 | ~~Slurm lifecycle~~ | removed together with the builder; the generic pieces it produced (per-host output framing, staged console rendering, job-log download, readable Ansible results) stay | — |

Deferred (documented, not in this iteration): monitoring dashboards &
capacity views (P3), benchmark framework (P3),
reports (P3), node "offline" liveness tracking, dnsmasq event push (polling
is fine at lab scale).

## Risk register

| Risk | Mitigation |
|---|---|
| sshpass/ssh edge cases (host key churn after reinstall) | `StrictHostKeyChecking=no` + `UserKnownHostsFile=/dev/null` already the project stance for a PXE lab |
| password leakage | secrets never enter `jobs.spec`/logs; passed via env (`SSHPASS`), deleted from memory at thread start; code-reviewed test asserts DB/log cleanliness |
| legacy nodes breaking | they keep existing; only *execution eligibility* is gated, with one-click Verify to restore it |
| single-worker job loss on restart | existing orphan reaper marks running jobs failed; onboarding is retryable |
