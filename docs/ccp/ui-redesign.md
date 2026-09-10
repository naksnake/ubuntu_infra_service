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
Rack View        ← new: auto-generated from hostname topology
ClusterShell
Ansible          ← redesigned: filesystem sources + inline fallback
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
  hardware one-liner (from `hardware`), last job.
- Row actions by state: `failed`/`discovered`/`unverified` → **Onboard**
  (credentials modal) / **Verify**; `managed` → **Rename** (one-click
  hostname modal), **Rescan HW**, **Delete**.
- Node detail drawer: full hardware facts, onboarding history (its jobs),
  state_detail on failure.
- The old free-form "Add node" card is demoted to the manual-add flow above.

### Clusters
Removed on 2026-09-10 together with the Slurm builder (see RFC amendment);
groups (`nodes.groups`) are the targeting mechanism.

### Rack View (new)
CSS-grid racks generated from `rack`/`sled` columns — no drawing, no config.
Cell = sled: name, role chip (gpu/cpu/…), state color, GPU count. Nodes
without topology fall into an "unracked" tray. Clicking a cell opens the node
detail drawer.

### ClusterShell / Ansible
- Shared target selector: node checkboxes and a group input; non-managed
  nodes are unselectable (greyed with their state).
- Ansible page: primary flow = pick source dir → pick playbook (scanned) →
  extra vars → run. Inline playbook textarea moves under "Ad-hoc playbook"
  (kept for one-offs, still not an editor — nothing is saved).

### Scripts
Shell snippets unchanged. Playbook tab shows existing playbook-kind scripts
read-only with "run" and a deprecation note pointing at Ansible sources.

## Visual language

Existing stylesheet extended with a state palette used identically on
Nodes, Rack View and Dashboard:

| state | color |
|---|---|
| managed | green |
| onboarding / running | blue (pulsing) |
| discovered / unverified | amber |
| failed | red |
| offline (future) | grey |

No new CSS framework; the current `style.css` design tokens continue.
