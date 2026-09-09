# UI Redesign Proposal

Principles (from the project charter): discovery first, automation first,
minimal configuration, operator friendly. Server-rendered Jinja + vanilla JS
stays — no SPA rewrite; the redesign is information architecture and
workflow, not technology.

## Navigation (new sidebar order)

```
Dashboard
Discovery        ← new: DHCP leases → import → onboard
Nodes            ← redesigned: lifecycle-centric inventory
Clusters         ← new
Rack View        ← new: auto-generated from hostname topology
ClusterShell
Ansible          ← redesigned: filesystem sources + inline fallback
Slurm            ← new (per-cluster lifecycle lives on the cluster page;
                    this entry lists Slurm clusters + states)
Jobs
Scripts          ← shell snippets only; playbook editing deprecated
Files
── Admin ──
Users
Audit Log
```

## Page-by-page

### Dashboard
Replace the four counters with the operator's actual questions:
- **Lifecycle funnel**: discovered / onboarding / managed / failed /
  unverified counts, each linking to the filtered Nodes page.
- New-on-the-network callout ("3 leases not in inventory → Discovery").
- Cluster cards: name, kind, members managed/total, Slurm state.
- Recent jobs (unchanged).

### Discovery (new)
Table of DHCP leases (IP, MAC, hostname, lease expiry) cross-referenced
against inventory: rows already imported show their node state; unknown rows
have a checkbox. Footer form: username + password + "Import & onboard
selected". That is the entire onboarding UX — no rack fields, no per-node
forms. A collapsed "Add node manually" card covers the no-DHCP case with the
same credential-first flow.

### Nodes (redesigned)
- Columns: state badge (color-coded), name, address, MAC, rack/sled/role,
  cluster, hardware one-liner (from `hardware`), last job.
- Row actions by state: `failed`/`discovered`/`unverified` → **Onboard**
  (credentials modal) / **Verify**; `managed` → **Rename** (one-click
  hostname modal), **Rescan HW**, **Delete**.
- Node detail drawer: full hardware facts, onboarding history (its jobs),
  state_detail on failure.
- The old free-form "Add node" card is demoted to the manual-add flow above.

### Clusters (redesigned: the cluster deploys itself)
One card per cluster: member table (add/remove from managed nodes), **Run on
this cluster** shortcuts (prefills ClusterShell/Ansible target), and for
`kind=slurm`:

```
INIT ▸ DISCOVER ▸ DEPLOY ▸ VALIDATE ▸ BENCHMARK ▸ REPORT ▸ MONITOR ▸ CLEANUP
[▶ Deploy Slurm automatically]  ☑ auto-deploy when members change
last run: job #42 · success · slurm_auto · 09 Sep 10:12
▸ Settings & manual steps
```

The primary action is one button. It queues a single `slurm_auto` job that
scans hardware, fixes hostnames, picks the controller (a node without GPUs is
preferred) and the install method (distro packages when every node offers the
same version, otherwise the same upstream release built from source on every
node), generates the configs, deploys, validates and runs the sbatch test.
The job page renders it as a numbered step list; the first failing stage
stops the run and carries its own reason. With *auto-deploy when members
change* on (the default), adding or removing a managed node, or a node in the
cluster finishing onboarding, re-runs the pipeline so the change is scheduled.

Everything that used to be a row of buttons lives in the collapsed
*Settings & manual steps* panel: controller (default auto), install mode,
version, tarball mirror (saved on the cluster, `PATCH /api/clusters/<id>`),
clean-reinstall, and the single steps Discover · Generate config · Deploy ·
Validate · sbatch test · Benchmark · Report · Monitor · Collect logs · Cleanup.
slurm.conf/gres.conf preview stays a read-only modal.

### Rack View (new)
CSS-grid racks generated from `rack`/`sled` columns — no drawing, no config.
Cell = sled: name, role chip (gpu/cpu/…), state color, GPU count. Nodes
without topology fall into an "unracked" tray. Clicking a cell opens the node
detail drawer.

### ClusterShell / Ansible
- Shared target selector gains a **Cluster** dropdown next to node
  checkboxes and group input; non-managed nodes are unselectable (greyed with
  their state).
- Ansible page: primary flow = pick source dir → pick playbook (scanned) →
  extra vars → run. Inline playbook textarea moves under "Ad-hoc playbook"
  (kept for one-offs, still not an editor — nothing is saved).

### Scripts
Shell snippets unchanged. Playbook tab shows existing playbook-kind scripts
read-only with "run" and a deprecation note pointing at Ansible sources.

## Visual language

Existing stylesheet extended with a state palette used identically on
Nodes, Rack View, Dashboard and Clusters:

| state | color |
|---|---|
| managed | green |
| onboarding / running | blue (pulsing) |
| discovered / unverified | amber |
| failed | red |
| offline (future) | grey |

No new CSS framework; the current `style.css` design tokens continue.
