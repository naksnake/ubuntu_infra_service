# Lab Services — PXE lab-in-a-box

A single-command PXE lab environment for Ubuntu/Debian hosts.  
Run `./deploy.sh` once — DHCP, TFTP, NAT, an HTTP file server, a web-based
iPXE boot-menu manager (with unattended **autoinstall** support), a health
dashboard, and a **Cluster Control Panel** (run ClusterShell commands and
Ansible playbooks across your nodes) all start automatically and survive reboots.

**Demo sample:** this repo is a complete, reproducible reference deployment.
The [worked example](#worked-example--dual-lan-mini-pc-intel-n97n100-16-gb)
below runs it on a dual-LAN Intel N97 mini PC with Ubuntu Desktop — follow
Steps 1–5 top to bottom and you end with a working PXE lab.

## Quickstart (TL;DR)

```bash
# 0. Two NICs: WAN cabled to your router, PXE cabled to the lab switch.
# 1. Static IP on the PXE NIC (Desktop example; details in Step 1):
sudo nmcli con add type ethernet ifname enp2s0 con-name lab-pxe \
     ipv4.method manual ipv4.addresses 192.168.100.1/24 \
     connection.autoconnect yes connection.autoconnect-priority 100 \
     connection.autoconnect-retries 0
sudo nmcli con up lab-pxe

# 2. Get the repo and configure:
git clone https://github.com/naksnake/ubuntu_infra_service.git
cd ubuntu_infra_service
cp .env.example .env          # edit: PXE_IFACE, WAN_IFACE, CCP_ADMIN_PASSWORD

# 3. Deploy (answer yes to the NAT and autostart prompts):
./deploy.sh
```

Then open `http://192.168.100.1:8091/` (iPXE Manager) to upload an ISO and
build your boot menu, `http://192.168.100.1:8060/` (Cluster Control Panel) to
run commands/playbooks across your nodes, and `http://192.168.100.1:8090/`
(Monitor) to watch service health and DHCP leases. Full details in the steps below.

---

## What you get

| Service | Container | Purpose |
|---|---|---|
| DHCP + PXE | `lab_dhcp` | Assigns IPs to lab clients, serves iPXE bootloaders |
| TFTP | `lab_tftp` | Delivers bootloader files to PXE clients |
| File server | `lab_webfs` | HTTP share for ISO images, kernels, initrds |
| iPXE Manager | `lab_ipxe_manager` | Web UI: upload boot files, edit the PXE boot menu, manage autoinstall profiles |
| Cluster Control Panel | `lab_ccp` | Web UI: node lifecycle management for AI/HPC labs — DHCP discovery → credential-validated onboarding → hardware discovery → clusters → file deployment, plus ClusterShell/Ansible execution, login/RBAC, job history and audit log |
| NAT | systemd `lab-nat` | Lets lab clients reach the internet via the host |
| Monitor | `lab_monitor` | Web dashboard: service health, DHCP lease lookup, file upload to the share |
| Docker API proxy | `lab_docker_proxy` | Read-only Docker API for the monitor (the raw socket is never mounted into a web-facing container) |

---

## Requirements

- **OS**: Ubuntu 22.04 / 24.04 / 26.04 (Server **or** Desktop) or Debian 12
  (Linux only — Docker Desktop on Mac/Windows does not support host networking)
- **NICs**: Two network interfaces
  - `PXE_IFACE` — connected to your lab switch (DHCP + TFTP will bind here)
  - `WAN_IFACE` — connected to the internet (used for NAT)
- **CPU / RAM**: 2 cores and 2 GB RAM is plenty (the whole stack is lightweight
  Flask + dnsmasq/tftpd containers); a dual-LAN mini PC like an Intel N97/N100
  box is a comfortable fit with generous headroom for serving ISOs
- **Root / sudo**: required for Docker install, IP forwarding, NAT setup, and systemd unit
- **Disk**: ~2 GB free for the container images plus space for your ISO images
  in `data/webfs_share/`

---

## Step 1 — Assign a static IP to the PXE interface

The host's PXE interface must have a static IP **before** you start the stack.  
This is the IP that DHCP clients will use as their gateway (`PXE_ROUTER_IP`).

Find your interface names first:
```bash
ip addr show
# Modern names look like enp1s0 / enp2s0 (dual-LAN mini PCs) or eno1, eth0…
# The port with your internet connection (has an IP already) is WAN_IFACE;
# the other port, cabled to the lab switch, is PXE_IFACE.
```

**Ubuntu Desktop (22.04 / 24.04 / 26.04 — NetworkManager):**

Desktop editions manage NICs with NetworkManager, so use `nmcli` (or the
Settings → Network GUI) instead of editing netplan files:

```bash
# replace enp2s0 with your actual PXE_IFACE name
sudo nmcli con add type ethernet ifname enp2s0 con-name lab-pxe \
     ipv4.method manual ipv4.addresses 192.168.100.1/24 \
     connection.autoconnect yes connection.autoconnect-priority 100 \
     connection.autoconnect-retries 0
sudo nmcli con up lab-pxe
ip addr show enp2s0             # confirm 192.168.100.1 is shown
```
This survives reboots. Leave the WAN port on its normal DHCP connection.

The three `autoconnect` settings matter: without them NetworkManager can drop
the profile when the lab switch power-cycles (carrier loss) and give up
re-activating it, or let a generic DHCP profile ("Wired connection 1") grab
the NIC instead — the classic "my static lab IP disappeared" failure.
`autoconnect-retries 0` means retry forever. If a generic profile keeps
stealing the lab NIC, pin it away:
```bash
nmcli -f NAME,DEVICE con show                 # see who owns which NIC
sudo nmcli con modify "Wired connection 1" connection.autoconnect no
```

**Ubuntu Server (netplan):**

Edit `/etc/netplan/01-lab.yaml` (create it if it doesn't exist):
```yaml
network:
  version: 2
  ethernets:
    enp2s0:                     # replace with your actual PXE_IFACE name
      dhcp4: false
      addresses: [192.168.100.1/24]
```

Apply:
```bash
sudo netplan apply
ip addr show enp2s0             # confirm 192.168.100.1 is shown
```

**Debian 12 (`/etc/network/interfaces`):**
```
auto eth1
iface eth1 inet static
    address 192.168.100.1
    netmask 255.255.255.0
```
```bash
sudo ifdown eth1 && sudo ifup eth1
```

> The exact IP (`192.168.100.1`) is the value you will enter for `PXE_ROUTER_IP`, `WEBFS_HOST_IP`, and `TFTP_SERVER_IP` in step 3.

**Desktop only — disable automatic suspend.** A desktop install may suspend
the machine after idle time, which takes DHCP/PXE/NAT down with it:
```bash
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type 'nothing'
```

---

## Step 2 — Clone the repo

```bash
git clone https://github.com/naksnake/ubuntu_infra_service.git
cd ubuntu_infra_service
chmod +x deploy.sh update-dhcp-range.sh
```

---

## Step 3 — Configure `.env`

Copy the example file:
```bash
cp .env.example .env
```

Open `.env` and fill in **your values**:
```ini
# ---- Interfaces ----
PXE_IFACE=enp2s0        # your lab NIC (from Step 1)
WAN_IFACE=enp1s0        # your internet NIC

# ---- IP addressing ----
PXE_RANGE_START=192.168.100.10   # first IP to hand out to lab clients
PXE_RANGE_END=192.168.100.200    # last IP to hand out to lab clients
PXE_NETMASK=255.255.255.0
PXE_LEASE_TIME=12h               # how long a client keeps its IP (45m, 12h, 1d, infinite)
PXE_ROUTER_IP=192.168.100.1      # host's PXE_IFACE IP (from Step 1)
WEBFS_HOST_IP=192.168.100.1      # same as PXE_ROUTER_IP
TFTP_SERVER_IP=192.168.100.1     # same as PXE_ROUTER_IP
DNS_SERVER=8.8.8.8

# ---- IPv6 (optional; the lab is IPv4-only when 0) ----
PXE_ENABLE_IPV6=0                # 1 = also serve DHCPv6 + router advertisements
PXE_ROUTER_IP6=fd00:100::1       # host's IPv6 on PXE_IFACE (assigned by the IP watchdog)
PXE_IPV6_RANGE_START=fd00:100::10
PXE_IPV6_RANGE_END=fd00:100::200
PXE_IPV6_PREFIX_LEN=64

# ---- Ports ----
WEBFS_PORT=8080
IPXE_MANAGER_PORT=8091
CCP_PORT=8060
MONITOR_PORT=8090
MONITOR_REFRESH=30               # dashboard auto-refresh interval (seconds)

# ---- iPXE Manager ----
# Admin account for the manager UI/API (HTTP Basic login). Setting a password
# enables auth; leave it blank for no login. /menu.ipxe and /autoinstall/
# always stay open for PXE clients.
IPXE_MANAGER_USER=admin
IPXE_MANAGER_PASSWORD=

# ---- Cluster Control Panel (CCP) ----
CCP_ADMIN_USER=admin
CCP_ADMIN_PASSWORD=YourPassword123   # at least 8 characters
CCP_DEMO=0                           # set 1 to seed a localhost demo node + sample scripts

# CCP_SECRET_KEY (Flask session signing) is auto-generated by deploy.sh.
# Leave it blank — deploy.sh fills it in.
CCP_SECRET_KEY=
```

---

## Step 4 — Run the deploy wizard

```bash
./deploy.sh
```

The wizard will ask you to confirm each setting, then it will:

1. Install Docker + Compose plugin (if not present)
2. Create the required data directories
3. Auto-generate `CCP_SECRET_KEY` and save it to `.env`
4. Offer to download iPXE boot binaries — defaults to **yes**. One per client
   architecture: `undionly.kpxe` (x86 BIOS), `ipxe.efi` (x86-64 UEFI) and
   `ipxe-arm64.efi` (ARM64 UEFI — Pi 4/5, ARM servers). Uses curl or wget,
   skips files already present, and a failed download only warns (copy the
   file into `services/tftp/tftpboot/` manually and re-run)
5. Build and start all containers with `docker compose up -d --build`
6. Offer to enable persistent NAT via a systemd unit (`lab-nat.service`)
7. Offer to install the **PXE static-IP watchdog** (`lab-ip-guard.timer`) —
   re-adds the lab IP within 30 s if NetworkManager drops it (carrier loss,
   suspend/resume, competing DHCP profile)
8. Offer to enable stack autostart on reboot via `lab-stack.service`

**Answer yes to the NAT, watchdog and autostart prompts** to get a fully
persistent, self-healing lab.

> The Control Panel is ready within a few seconds of the containers starting —
> log in with `CCP_ADMIN_USER` / `CCP_ADMIN_PASSWORD` from your `.env`.

---

## Step 5 — Verify everything is running

### Check containers
```bash
docker ps
```
Expected output — all seven containers should show `Up`:
```
CONTAINER ID   IMAGE                STATUS                     NAMES
...            lab_ipxe_manager     Up X minutes (healthy)     lab_ipxe_manager
...            lab_ccp              Up X minutes (healthy)     lab_ccp
...            lab_monitor          Up X minutes (healthy)     lab_monitor
...            lab_docker_proxy     Up X minutes               lab_docker_proxy
...            lab_webfs            Up X minutes (healthy)     lab_webfs
...            lab_dhcp             Up X minutes (healthy)     lab_dhcp
...            lab_tftp             Up X minutes (healthy)     lab_tftp
```

### Verify DHCP
From a client on the lab network (or use a VM on the lab segment):
```bash
# On the lab client — check it received an IP in the range you configured:
ip addr show
# Should show an IP between PXE_RANGE_START and PXE_RANGE_END
```

Check dnsmasq logs to see leases being issued:
```bash
docker logs lab_dhcp | grep -i DHCP
# Example: DHCP, offered 192.168.100.25, enp2s0 ...
```

### Verify NAT
From a lab client that received a DHCP IP:
```bash
ping -c 3 8.8.8.8           # should reach the internet
curl -s https://example.com  # should return HTML
```

If clients can ping the lab gateway (`192.168.100.1`) but not the internet, check NAT:
```bash
sudo systemctl status lab-nat.service
sudo iptables -S DOCKER-USER          # lab PXE→WAN ACCEPT rules live here
sudo iptables -t nat -S POSTROUTING   # MASQUERADE rule for the WAN interface
```
> NAT rules are inserted into Docker's `DOCKER-USER` chain — an ACCEPT in a
> separate table would be overridden by Docker's `FORWARD` policy of `DROP`.

### Verify the file server
Open `http://192.168.100.1:8080/` in a browser — the webfs web UI lists the
`/files/` share and the static iPXE scripts, with per-file **Download** and
**Copy URL** buttons (use Copy URL when building boot entries by hand).

Or from the command line:
```bash
curl -fsS "http://192.168.100.1:8080/files/"
# Should return an HTML directory listing (empty until you copy files in)
```

### Verify the iPXE Manager
Open a browser and go to:
```
http://192.168.100.1:8091/
```
You should see the Files / Boot Menu / iPXE Preview tabs. PXE clients fetch
their boot menu from `http://192.168.100.1:8091/menu.ipxe` — see
[Adding boot images](#adding-boot-images-ipxe-manager) below.

### Verify the monitor dashboard
Open a browser and go to:
```
http://192.168.100.1:8090/
```
The dashboard now requires **login**. Sign in with the admin account (it reuses
`CCP_ADMIN_USER` / `CCP_ADMIN_PASSWORD` from your `.env`). You will see:
- **Services** — all container names, status (running/exited), health check
  result, uptime, restart count. Click the heading to collapse or expand the
  table — the choice sticks across the page's auto-refresh (the container
  count stays visible in the heading while collapsed)
- **DHCP Leases** — IP address, MAC address, hostname, lease expiry, and time remaining for every active lease
- **Quick links** — one-click access to the other web UIs, plus an
  **Upload to file server** card (admin role only): click it or drop files on
  it and they are stored in the monitor's own space in the share,
  `data/webfs_share/monitor/` (URLs under `/files/monitor/`), keeping
  dashboard uploads separate from files managed in the iPXE Manager.
  **Whole folders work too** — use the card's *upload a whole folder* link or
  drop a directory on the card, and its structure is recreated under
  `/files/monitor/<folder>/…` (dotfile junk like `.DS_Store` is skipped).
  Uploads are forwarded to the iPXE Manager, so an ISO gets the automatic
  kernel+initrd extraction and a disabled boot entry exactly as if uploaded
  in the manager UI — even when `IPXE_MANAGER_PASSWORD` is set. Files stream
  straight into the share (no scratch copy in any container), and the page's
  auto-refresh pauses while a file is being picked or uploaded.
- **File Server** — lists everything in the `/files/` share (dashboard
  uploads carry a `monitor` badge) with per-file **Download** and
  **Copy URL**, a per-folder **Download .zip** (the whole folder streams as
  a ZIP archive — nothing is staged on disk or in RAM), and — for admins —
  **Remove**. Download always *saves* the file whatever its type — a
  `.run` installer or a text file streams with an attachment header
  (resumable) instead of rendering in the browser tab. Copy URL yields a working download link built for the address
  you're browsing from (lab-side viewers get the lab IP, WAN-side viewers
  the WAN IP), and copying works on plain-HTTP pages too. Removing an ISO
  also removes its extracted kernel/initrd folder. The viewer role sees the
  list read-only (downloads included).

Use the search box to quickly find a host by IP, MAC, or hostname.  
The page auto-refreshes every 30 seconds. A JSON API is available at `/api/status` (login required).

**Login & roles.** The dashboard requires a login. Sessions last **30 minutes**
(sliding — activity resets the timer; `MONITOR_SESSION_MINUTES` to change) and
there is a **Log out** button. Accounts carry a role shown in the top bar:
**admin** (the default account) and an optional read-only **viewer** (set
`MONITOR_VIEWER_PASSWORD` to enable). `/healthz` stays open for the container
health check.

### Verify the Cluster Control Panel
Open a browser and go to:
```
http://192.168.100.1:8060/
```
Log in with `CCP_ADMIN_USER` / `CCP_ADMIN_PASSWORD` from your `.env`. From here you can
import machines discovered from DHCP leases, onboard them with a username+password
(CCP validates access and installs its SSH key before a node counts as managed),
run ClusterShell commands and Ansible playbooks across nodes and clusters, deploy
files to them, and review job history and the audit log.
See [Cluster Control Panel](#cluster-control-panel-node-lifecycle) below.

If it isn't up yet, check its logs:
```bash
docker logs -f lab_ccp
```

---

## Adding boot images (iPXE Manager)

Open the iPXE Manager in a browser:
```
http://192.168.100.1:8091/
```

**To make a new OS bootable over the network:**

1. **Files tab** — upload your ISO, kernel (`vmlinuz`), or initrd
   (drag & drop or click to browse; files are stored in `data/webfs_share/`).
   **Uploading an ISO does the rest automatically**: the manager extracts the
   kernel + initrd out of the image into a folder named after it
   (`data/webfs_share/<iso-name>/vmlinuz` + `initrd`) and creates a ready-made
   **Kernel + initrd** boot entry whose command line hands the OS the ISO's
   HTTP URL — the UEFI-friendly path, no sanboot involved:
   ```
   kernel http://<server>:8080/files/<iso-name>/vmlinuz initrd=initrd ip=dhcp url=http://<server>:8080/files/<name>.iso
   initrd http://<server>:8080/files/<iso-name>/initrd
   boot
   ```
   The auto-created entry starts **disabled** so the boot menu never changes
   behind your back — enable it when you're ready.
2. **Boot Menu tab** — click **+ Add Entry**, give it a name, pick the boot type:
   - **Kernel + initrd** — fetched over HTTP; works on **BIOS and UEFI**
     (this is the modern, recommended path)
   - **Chainload URL** — point at another `.ipxe` script
3. In each file field the base URL (`http://<server>:8080/files/`) is fixed —
   you type or pick **only the filename**. A live preview under the form shows
   both the full URL and the exact iPXE lines the entry will generate.

### UEFI network install from an ISO or rootfs over HTTP

Because UEFI can't sanboot an ISO, boot the installer's **kernel + initrd**
and hand the OS the HTTP URL of the ISO/rootfs on the kernel command line —
the exact parameter depends on the distro. The editor lists every uploaded
file's URL with a copy button so you can paste the right one in. Examples:

```
# Ubuntu autoinstall (casper fetches the squashfs/ISO over HTTP)
ip=dhcp url=http://192.168.100.1:8080/files/ubuntu-24.04-live-server-amd64.iso autoinstall

# Debian/Ubuntu with a squashfs rootfs
boot=live fetch=http://192.168.100.1:8080/files/filesystem.squashfs ip=dhcp
```

> **ISO shortcut:** uploading a `.iso` auto-creates a disabled
> **Kernel + initrd** entry built from the boot files extracted out of the
> ISO — it works on UEFI *and* BIOS, so there is no separate sanboot type
> (sanboot was BIOS-only and cannot work on UEFI). The extractor knows the
> standard layouts (Ubuntu/Debian `casper/` & `live/`, Debian installer
> `install.amd/`, Fedora/RHEL `images/pxeboot/`, openSUSE, Arch); an ISO with
> no recognizable kernel+initrd pair gets no entry — add a Kernel + initrd
> entry by hand instead. The default command line `ip=dhcp url=<iso-url>`
> fits Ubuntu live ISOs — for other distros edit it (e.g. `inst.repo=` for
> Fedora/RHEL). Bare kernels are never auto-added because they need a
> matching initrd and command line. Deleting an ISO also removes its
> extracted folder.

### Boot order

The order in the Boot Menu tab is the order clients see, and the top enabled
entry (badged **default**) boots automatically after a 30-second timeout.
Use the ▲▼ arrows to change it; disabled entries are hidden from clients.

Every change is live immediately — the next PXE boot picks it up with no
container restart. You can rename, enable/disable, or delete entries at any
time; the **iPXE Preview** tab shows the exact script clients receive.

> You can also copy files straight into `data/webfs_share/` from the shell —
> they appear in the manager's file list and dropdowns automatically.

To password-protect the manager, set `IPXE_MANAGER_PASSWORD` in `.env` — the
browser then asks for the `IPXE_MANAGER_USER` / password account (HTTP Basic;
username defaults to `admin`). PXE clients can always fetch `/menu.ipxe` and
`/autoinstall/…` without a password, and the Monitor dashboard forwards the
same account automatically for its uploads and file removals.

---

## Unattended installs (autoinstall)

The iPXE Manager can drive a fully **unattended OS install** using cloud-init's
NoCloud datasource — the standard Ubuntu Server *autoinstall* flow — so a machine
PXE-boots and installs itself with no keyboard interaction.

> ⚠️ **An autoinstall ERASES the target disk.** Entries that use a profile are
> flagged `[AUTOINSTALL — ERASES DISK]` in the boot menu and are best left
> **disabled** until you actually intend to reinstall that machine.

**How it works:** you create an *autoinstall profile* — a cloud-init `user-data`
document — and attach it to a **Kernel + initrd** boot entry. The manager serves
the profile at `http://<server>:8091/autoinstall/<id>/` (`user-data` + `meta-data`)
and automatically appends `autoinstall ds=nocloud-net;s=http://<server>:8091/autoinstall/<id>/`
to that entry's kernel command line. These seed URLs stay reachable even when
`IPXE_MANAGER_PASSWORD` is set, so the installer can fetch them.

**Steps:**

1. **Files tab** — upload the Ubuntu **live-server** ISO. The manager extracts
   its `vmlinuz` + `initrd` and creates a disabled **Kernel + initrd** entry
   for it automatically.
2. **Autoinstall tab** → **+ New Profile**. The editor is pre-filled with a
   standard Ubuntu autoinstall template (identity, storage `layout: direct`, SSH
   server). Edit the hostname, user, password hash (`mkpasswd -m sha-512`), disk
   layout, and packages. It's validated as YAML on save.
3. **Boot Menu tab** → edit the auto-created **Kernel + initrd** entry (its
   command line already reads
   `ip=dhcp url=http://192.168.100.1:8080/files/ubuntu-24.04.1-live-server-amd64.iso`)
   and pick your profile in **Autoinstall profile**. The live preview shows the
   exact kernel line, including the appended seed URL.
4. Enable the entry only when you're ready; the next PXE boot of that machine
   installs Ubuntu unattended per your profile.

The **iPXE Preview** tab always shows the exact script clients receive.

---

## Cluster Control Panel (node lifecycle)

Open the Control Panel:
```
http://192.168.100.1:8060/
```
Log in with `CCP_ADMIN_USER` / `CCP_ADMIN_PASSWORD`. CCP is a lifecycle
management panel for AI/HPC lab clusters (design docs in `docs/ccp/`):

- **Discovery** — every machine that took a DHCP lease from the lab is listed
  (IP, MAC, hostname) and cross-referenced against the inventory. Select the
  new ones, enter one username+password, and they are imported and onboarded
  in bulk. Manual node entry remains as a fallback.
- **Onboarding with validated access** — a node becomes **managed** only after
  CCP (1) verifies the credentials, (2) installs its SSH key
  (auto-generated at `data/ccp/ssh/id_ccp`), and (3) confirms command
  execution over key auth. Passwords are used once, never stored. Failures
  land in a retryable `failed` state with the reason; only managed nodes can
  run jobs.
- **Hardware discovery** — CPU, memory, disks, NICs, GPUs (nvidia-smi/lspci),
  OS and InfiniBand facts are collected automatically after onboarding and on
  demand, and drive the inventory and rack view.
- **Hostname-driven topology** — names like `rack0_sled1_gpu` parse into
  rack/sled/role automatically (no rack database), and the **Rack View** page
  draws itself from them. One-click rename runs `hostnamectl set-hostname`
  (+ `/etc/hostname`, `/etc/hosts`) on the node and refreshes the inventory
  immediately.
- **Clusters** — first-class groups of managed nodes that act as execution
  targets for ClusterShell, Ansible and file deployment.
- **Deploy files** — push staged files to groups or individual nodes with one
  click (Ansible copy or `clush --copy`), with per-host results.
- **ClusterShell** — run a shell command across selected nodes/groups/clusters
  in parallel and see per-node output live.
- **Ansible** — playbooks are developed *outside* CCP and consumed from local
  source directories (`CCP_ANSIBLE_DIRS`, default `data/ccp/ansible`, with
  `roles/`, `group_vars/`, `host_vars/` honored); an inventory is generated
  from the selected targets automatically. Ad-hoc inline playbooks still work.
- **Login + RBAC** — three roles: `viewer` (read-only), `operator` (run jobs,
  manage nodes/clusters/scripts/files), `admin` (everything + user management
  + audit log).
- **Job history** — every run (commands, playbooks, onboarding, hardware
  scans, renames, file deployments) is recorded with status, exit code, and full
  output.
- **Files** — per-user file storage (kickstart snippets, tarballs, etc.).
- **Audit log** — every login and state-changing action is recorded (admin-only).

> **Try it without any real hosts:** set `CCP_DEMO=1` in `.env` before the first
> run to seed a self-contained `localhost` node plus sample scripts, so you can
> exercise ClusterShell and Ansible immediately.

---

## DHCP: lease time, fixed IPs, and the address pool

### How long does a client keep its IP? (lease time)

Every dynamic IP is a **lease** with a time limit, set by `PXE_LEASE_TIME` in
`.env` (default `12h`). The lifecycle:

- At **50%** of the lease (6h with the default) the client automatically asks
  the server to **renew**. A client that stays online renews forever and keeps
  the *same* IP indefinitely — the lease time is not a maximum ownership time.
- If renewal gets no answer, the client retries by broadcast at **87.5%**
  (*rebind*), and releases the address only when the lease fully expires.
- If the client goes **offline**, its IP stays reserved until the lease
  expires; only then can the pool give it to a different machine. dnsmasq
  also prefers to re-issue the same IP to a returning MAC when it is free.

Accepted formats: seconds (`3600`), `45m`, `12h`, `1d`, or `infinite`
(minimum `2m`). Short leases (e.g. `45m`) recycle addresses quickly in a busy
PXE lab; long leases (`1d`+) keep quiet networks stable. Apply a change with:

```bash
docker compose up -d --force-recreate dhcp
```

Live leases (IP, MAC, hostname, expiry) are visible on the Monitor dashboard
or in `data/dnsmasq.leases` (first column = expiry as a Unix timestamp;
`0` means an infinite lease).

### Fix an IP address to a MAC (static reservation)

To make a machine always get the same IP, add one line per machine to
`services/dhcp/static-hosts.conf`:

```
# <mac>,<ip>[,<hostname>][,<lease time>]
aa:bb:cc:dd:ee:01,192.168.100.201,node-01
aa:bb:cc:dd:ee:02,192.168.100.202,node-02,24h
aa:bb:cc:dd:ee:03,192.168.100.203,storage-01,infinite
```

Then apply with:

```bash
docker compose restart dhcp
```

Rules of thumb:

- Reserve addresses **inside the PXE subnet but outside the dynamic pool**
  (with the defaults: pool is `.10–.200`, so reserve `.201–.254`). This
  guarantees the pool can never hand a reserved address to another machine.
- Find a machine's MAC on the Monitor dashboard's lease table, in
  `docker logs lab_dhcp`, or with `ip link` on the client itself.
- A client that is already online switches to its reserved IP at its next
  renewal (at the latest, half the lease time) or immediately after a reboot.

### Changing or expanding the DHCP range

To grow (or move) the pool **within the same subnet**, you do not need to
restart the full stack — only the DHCP container is recycled:

```bash
# IPv4 pool:
./update-dhcp-range.sh 192.168.100.10 192.168.100.250

# IPv6 pool (used when PXE_ENABLE_IPV6=1 — the family is auto-detected):
./update-dhcp-range.sh fd00:100::10 fd00:100::4ff

# Interactive (IPv4, plus IPv6 when it is enabled):
./update-dhcp-range.sh
```

Existing leases are not affected until they expire.

A `/24` netmask (`255.255.255.0`) allows at most 254 hosts (`.1–.254`);
remember to leave room for the server (`.1` by default) and your static
reservations. If you need **more addresses than one /24 provides**, you must
widen the subnet itself, e.g. to a /23 (`192.168.100.0–192.168.101.255`,
510 hosts):

1. In `.env`: set `PXE_NETMASK=255.255.254.0` and widen the pool, e.g.
   `PXE_RANGE_START=192.168.100.10`, `PXE_RANGE_END=192.168.101.250`.
2. Update the host's own address on the lab NIC to match
   (`addresses: [192.168.100.1/23]` in the netplan config from Step 1),
   then `sudo netplan apply`.
3. Re-render and restart DHCP: `docker compose up -d --force-recreate dhcp`.
4. Clients pick the new mask up as their leases renew; reboot or re-plug a
   client to force it immediately.

### IPv6 on the lab segment (optional)

The lab is **IPv4-only by default**: `PXE_ENABLE_IPV6=0` means dnsmasq serves
DHCPv4 only, and the PXE IP watchdog keeps IPv6 switched off on `PXE_IFACE`
entirely — lab machines get no v6 path to the server, not even link-local.

To hand out IPv6 addresses too, set in `.env`:

```ini
PXE_ENABLE_IPV6=1
PXE_ROUTER_IP6=fd00:100::1        # host's IPv6 on the lab NIC
PXE_IPV6_RANGE_START=fd00:100::10 # first DHCPv6 address
PXE_IPV6_RANGE_END=fd00:100::200  # last DHCPv6 address
PXE_IPV6_PREFIX_LEN=64
```

then apply with `docker compose up -d --force-recreate dhcp` (plus
`sudo systemctl start lab-ip-guard.service` to assign `PXE_ROUTER_IP6`
immediately, or wait up to 30 s for the watchdog timer). dnsmasq then runs
stateful DHCPv6 and router advertisements on the lab interface; v6 leases
appear in the same Monitor lease table with the client DUID in the MAC
column. The `fd00::/8` prefix is private ULA space — pick your own random
prefix (RFC 4193) if this lab ever connects to another network. To change
the v6 pool later, `./update-dhcp-range.sh fd00:100::10 fd00:100::4ff`
updates `.env` and recycles only the DHCP container.

Notes:
- **Lab IPv6 stays lab-local.** NAT and forwarding remain IPv4-only, so v6
  never becomes a route around the IPv4 firewall; clients reach the internet
  via IPv4 exactly as before.
- If you skipped the IP watchdog during deploy, assign the address yourself:
  `sudo ip addr add fd00:100::1/64 dev <PXE_IFACE>` — without an address in
  the DHCPv6 prefix on that interface, dnsmasq ignores the v6 range.
- PXE network boot keeps using IPv4; the v6 addresses are for the installed
  systems and lab-internal traffic.

---

## Day-to-day operations

```bash
# View all container statuses and health
docker ps

# Follow logs for a specific service
docker logs -f lab_dhcp
docker logs -f lab_monitor
docker logs -f lab_ccp
docker logs -f lab_ipxe_manager

# Restart a single service
docker compose restart dhcp

# Stop the entire stack
docker compose down

# Start the entire stack
docker compose up -d

# Rebuild after changing a Dockerfile
docker compose up -d --build dhcp
```

### Clean and rebuild from scratch

When an image is stale or broken (or you just want a fresh start), use the
deploy script's subcommands instead of hunting down containers by hand:

```bash
# Stop the stack; remove its containers, networks and locally built images.
# Keeps .env and ./data (ISOs, leases, CCP db, certs).
./deploy.sh clean

# clean + rebuild every image with --no-cache + start the stack again
./deploy.sh rebuild
```

Re-running plain `./deploy.sh` also detects an existing stack and asks
*"Remove old containers + built images before deploying?"* — answer **Y**
(the default) for a clean re-deploy straight from the wizard.

For a true factory reset, run `./deploy.sh clean` and then delete `./data`
(this erases uploaded ISOs, DHCP leases, the CCP database and certificates).

---

## HTTPS for the file share (optional)

The webfs share can also be served over TLS — webfsd has native HTTPS
support. This is meant for humans downloading files from a browser;
**PXE boot keeps using plain HTTP** (stock iPXE binaries will not trust a
self-signed certificate, and netboot needs no TLS on an isolated lab
segment).

1. Enable the compose profile in `.env`:
   ```
   COMPOSE_PROFILES=https
   WEBFS_HTTPS_PORT=8443
   ```
2. Re-run `./deploy.sh` (answer **n** to the wizard to keep your `.env`).
   It generates a self-signed certificate for `WEBFS_HOST_IP` into
   `data/certs/webfs.pem` and starts the extra `lab_webfs_https` container.
   Or do it by hand:
   ```bash
   mkdir -p data/certs
   openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
     -subj "/CN=192.168.100.1" -addext "subjectAltName=IP:192.168.100.1" \
     -keyout data/certs/webfs.key -out data/certs/webfs.crt
   cat data/certs/webfs.crt data/certs/webfs.key > data/certs/webfs.pem
   docker compose up -d   # COMPOSE_PROFILES=https must be set in .env
   ```
3. Browse `https://192.168.100.1:8443/` — same web UI and `/files/` share as
   the HTTP listener. Your browser will warn once about the self-signed cert.

To use a real certificate instead, replace `data/certs/webfs.pem` with your
own **chained PEM (certificate first, then the private key)** and
`docker compose restart webfs-https`. To turn HTTPS off again, clear
`COMPOSE_PROFILES` in `.env` and run `docker compose --profile https down`
followed by `docker compose up -d`.

---

## Autostart and NAT after reboot

After running `deploy.sh` with autostart and NAT enabled:

```bash
# Check autostart is enabled
sudo systemctl status lab-stack.service

# Check NAT is enabled
sudo systemctl status lab-nat.service
```

```bash
# Check the PXE static-IP watchdog
sudo systemctl status lab-ip-guard.timer
journalctl -t lab-ip-guard          # every automatic restoration is logged
```

All units start automatically at boot. To enable them manually if you skipped the prompts:
```bash
sudo systemctl enable --now lab-stack.service
sudo systemctl enable --now lab-nat.service
sudo systemctl enable --now lab-ip-guard.timer
```

---

## Worked example — dual-LAN mini PC (Intel N97/N100, 16 GB)

A fanless dual-LAN mini PC (e.g. Limyee BOX1212: Intel N97, 2× 2.5 GbE,
16 GB DDR5, 1 TB SSD) running Ubuntu Desktop is an ideal appliance for this
stack. Complete recipe:

1. **Install Ubuntu Desktop** on the SSD (22.04 / 24.04 / 26.04 all work).
   The 2.5 GbE ports (Intel i226-class) are supported out of the box.
2. **Cable it**: LAN port 1 → your office router/internet (`WAN_IFACE`),
   LAN port 2 → the lab switch where PXE machines live (`PXE_IFACE`).
   Confirm names with `ip addr show` — typically `enp1s0` / `enp2s0`.
3. **Static IP on the lab port** with `nmcli` and **disable auto-suspend**
   (both shown in Step 1 above).
4. **Clone + configure + deploy** (Steps 2–4). In `.env`:
   `WAN_IFACE=enp1s0`, `PXE_IFACE=enp2s0`, everything else default.
   Answer **yes** to the NAT and autostart prompts.
5. **BIOS tip**: enable *Restore on AC Power Loss* so the box comes back up
   after an outage — the systemd units restart the whole stack on boot.

Resource fit on 16 GB / 4 cores:

| Component | Idle RAM |
|---|---|
| Cluster Control Panel (Flask + ansible-core) | ~150 MB |
| DHCP, TFTP, webfs, monitor, iPXE Manager | < 300 MB combined |
| Ubuntu Desktop (GNOME) | ~1.5–2 GB |
| **Headroom** | **~9 GB** for file cache while serving ISOs |

The N97's 4 cores handle the full stack plus several concurrent PXE
installs; the 2.5 GbE lab port is the practical limit for parallel image
downloads, not the CPU. Store ISOs in `data/webfs_share/` on the SSD.

---

## Security

The stack is built for an **isolated lab segment behind a trusted admin host**.
These are the layers it ships with and the knobs you should set:

### Network / NAT (applied automatically by `deploy.sh`)

- **Stateful NAT only** — traffic from the WAN side can never *initiate* a
  connection into the lab; only replies to lab-originated connections are
  forwarded.
- **Subnet-scoped, anti-spoofing rules** — forwarding and masquerading only
  apply to packets sourced from the lab subnet, and anything else arriving on
  the PXE interface is dropped. Rules live in Docker's `DOCKER-USER` chain
  (re-run `./deploy.sh` or `sudo systemctl restart lab-nat.service` after
  updating to refresh them).
- **Kernel hardening** (`/etc/sysctl.d/99-lab-nat.conf`) — reverse-path
  filtering, ICMP-redirect and source-route packets ignored, spoofed
  ("martian") packets logged to the kernel log for detection
  (`journalctl -k | grep martian`).

### Web UI exposure — the most important knob

By default the four web UIs are published on **all** host interfaces,
including the WAN side. Set in `.env`:

```ini
UI_BIND=192.168.100.1        # your PXE_ROUTER_IP
IPXE_MANAGER_USER=admin      # iPXE Manager admin account…
IPXE_MANAGER_PASSWORD=...    # …never leave the boot-menu editor open
```

and re-run `docker compose up -d`. The UIs are then reachable only from the
lab segment and the server itself; from your office machine, tunnel in:
`ssh -L 8091:192.168.100.1:8091 <server>`. An unprotected iPXE Manager is the
crown jewel for an attacker — whoever edits the boot menu controls every
machine that PXE-boots.

> **ufw users:** Docker-published container ports **bypass ufw** (Docker's
> NAT rules run before ufw's INPUT chain), so `ufw deny 8091` does *not*
> protect them — use `UI_BIND` instead. ufw still works for host-network
> services: a good baseline is `default deny incoming`, `allow in on
> <PXE_IFACE>`, and SSH allowed only from your admin network.

### Container privilege boundaries

- **No web-facing container holds the Docker socket.** The monitor's Services
  table needs container status, but the raw `docker.sock` API is
  root-equivalent on the host (a `:ro` mount only protects the socket *file*,
  not what the API will do). The dashboard therefore talks to
  `lab_docker_proxy` — an HAProxy-based filter
  ([tecnativa/docker-socket-proxy](https://github.com/Tecnativa/docker-socket-proxy))
  that permits exactly the read-only container/image endpoints the dashboard
  uses and denies everything else. The proxy lives on an `internal:`
  compose network that only the monitor can reach, so a compromised container
  elsewhere in the stack (or a bug in the monitor itself) cannot escalate
  through the Docker API to the host or to other containers' credentials.
- **The CCP job runner re-validates node rows before use.** Node
  names/addresses/SSH users are allowlist-checked at the API when added *and*
  again inside the executor before they are written into a ClusterShell
  invocation or a generated Ansible inventory — a database row edited outside
  the API (the SQLite file lives on a host bind mount) is skipped with a note
  in the job log instead of executed.

### Login protection & sessions

- **Brute-force lockout** on all three logins (Monitor, Control Panel, iPXE
  Manager): after 5 failed attempts (10 for the manager's HTTP Basic auth)
  from one address within 15 minutes, further attempts get HTTP 429 until the
  window rolls over. Tune with `LOGIN_FAIL_LIMIT` / `LOGIN_FAIL_WINDOW`.
  PXE-facing endpoints (`/menu.ipxe`, autoinstall seeds) are never locked out.
- **Independent sessions per UI** — each app uses its own session cookie
  (`lab_monitor_session`, `lab_ccp_session`), so logging in to one UI no
  longer logs you out of another. Cookies are `HttpOnly` + `SameSite=Lax`;
  every state-changing request requires a CSRF token; all responses carry
  `X-Content-Type-Options`, `X-Frame-Options: DENY` and a no-referrer policy.

### Detection — where to look

| Signal | Where |
|---|---|
| Web logins, lockouts, uploads, file removals | `docker logs lab_monitor` (audit lines) |
| Control Panel logins + every state change | CCP **Audit log** page (admin) / SQLite db |
| Failed iPXE Manager auth attempts | `docker logs lab_ipxe_manager` |
| Spoofed/martian packets | `journalctl -k \| grep -i martian` |
| Unexpected DHCP clients | Monitor dashboard lease table |
| Container restarts / unhealthy services | Monitor dashboard **Services** table |

### What stays cleartext (by design) — and what that means

PXE itself (DHCP/TFTP/HTTP boot) is unencrypted; anyone with a port on the
**lab switch** can capture kernels, ISOs and autoinstall seeds — including
the password **hashes** inside autoinstall profiles. Treat lab-switch access
as equivalent to console access: use a dedicated, physically controlled
switch (or an isolated VLAN), use strong `mkpasswd -m sha-512` hashes and
rotate the first-boot password, and give humans the HTTPS listener
(`COMPOSE_PROFILES=https`) for browsing the share. IPv6 is not a bypass:
by default it is disabled on the lab interface altogether
(`PXE_ENABLE_IPV6=0`), and even when enabled, NAT and forwarding stay
IPv4-only.

---

## Troubleshooting

**DHCP clients get no IP**
- Confirm `PXE_IFACE` has the static IP: `ip addr show <PXE_IFACE>`
- Check dnsmasq started: `docker logs lab_dhcp | head -20`
- Confirm no other DHCP server is on the lab segment: `sudo nmap --script broadcast-dhcp-discover`

**The static IP on the PXE interface keeps disappearing**
- Usual cause on Ubuntu Desktop: NetworkManager deactivates the profile when
  the lab NIC loses carrier (lab switch powered off / rebooted, cable
  unplugged, suspend/resume) and either gives up re-activating it or lets a
  generic DHCP profile claim the NIC when the link returns.
- Quick fix now: `sudo nmcli con up lab-pxe` (or `sudo systemctl start
  lab-ip-guard.service` if the watchdog is installed).
- Make the profile resilient (see Step 1):
  ```bash
  sudo nmcli con modify lab-pxe connection.autoconnect yes \
       connection.autoconnect-priority 100 connection.autoconnect-retries 0
  nmcli -f NAME,DEVICE con show     # a "Wired connection 1" on the lab NIC?
  sudo nmcli con modify "Wired connection 1" connection.autoconnect no
  ```
- Install the **watchdog** if you skipped the prompt — re-run `./deploy.sh`
  and answer yes to "Install the PXE static-IP watchdog". It checks every
  30 s and re-adds the address whenever it is missing; each restoration is
  visible in `journalctl -t lab-ip-guard`, so you can also see *how often*
  (and roughly when) the address is being lost.
- Desktop machines: make sure auto-suspend is disabled (Step 1) — suspend
  takes the NIC down with it.

**NAT not working (clients can ping gateway but not internet)**
- Check IP forwarding is on: `cat /proc/sys/net/ipv4/ip_forward` (must be `1`)
- Check the ACCEPT rules: `sudo iptables -S DOCKER-USER | grep -E "$PXE_IFACE|$WAN_IFACE"`
- Re-apply / restart NAT service: `sudo systemctl restart lab-nat.service`

**Cluster Control Panel won't load or log in fails**
- Check the container is up: `docker ps | grep lab_ccp`
- Check its logs: `docker logs lab_ccp 2>&1 | tail -30`
- Confirm `CCP_ADMIN_PASSWORD` is set in `.env` (the admin is seeded on first run only)

**Monitor upload card fails or shows "iPXE Manager unreachable"**
- The card checks the monitor→manager link when the page loads and shows the
  exact reason on the card; the same message appears in
  `docker logs lab_monitor`.
- Rebuild and recreate **both** containers together, then hard-reload the
  dashboard (Ctrl+Shift+R) so the browser drops the old page's JavaScript:
  ```bash
  docker compose up -d --build monitor ipxe-manager
  ```
- Test the internal link by hand (expect `200`):
  ```bash
  docker exec lab_monitor python -c \
    "import requests; print(requests.get('http://ipxe-manager:8091/menu.ipxe', timeout=5, proxies={'http': None}).status_code)"
  ```
- Changed `IPXE_MANAGER_PASSWORD` in `.env`? Recreate both containers — the
  monitor sends that password with every upload.
- HTTP proxies injected into containers by the Docker daemon (common on
  corporate networks) are ignored for this internal call.

**Containers restart repeatedly**
- Check for missing `.env` values: `docker logs lab_dhcp | head -5`
- Re-run `./deploy.sh` — it prompts "Run interactive configuration wizard now? (Y/n)";
  press Enter to re-run the wizard, or answer **n** to keep your existing `.env` as-is

---

## File layout

```
ubuntu_infra_service/
├── deploy.sh                    # Run this once to set everything up
├── update-dhcp-range.sh         # Change DHCP pool without restarting the stack
├── docker-compose.yml
├── .env.example                 # Copy to .env and edit before running deploy.sh
│
├── ipxe/                        # Static iPXE scripts (manual fallbacks, served by webfs)
│   ├── default.ipxe             # Chains to the iPXE Manager's live menu
│   ├── menu.ipxe                # Static fallback menu
│   └── linux-kernel-initrd.ipxe # UEFI-friendly kernel+initrd boot
│
├── services/
│   ├── dhcp/                    # dnsmasq DHCP container (PXE pointers, no TFTP)
│   │   └── static-hosts.conf    # Fixed IP-to-MAC reservations (edit + restart dhcp)
│   ├── tftp/                    # tftpd-hpa bootloader delivery container
│   ├── webfs/                   # HTTP file server container
│   ├── ipxe_manager/            # Web UI: file uploads + PXE boot menu + autoinstall profiles
│   ├── monitor/                 # Flask dashboard (service health + DHCP leases)
│   └── ccp/                     # Cluster Control Panel (ClusterShell + Ansible web UI)
│       ├── app.py               # Flask app: auth/RBAC, nodes, jobs, scripts, files, audit
│       ├── db.py                # SQLite persistence
│       └── executor.py          # ClusterShell + ansible-playbook job runner
│
└── data/                        # Runtime data — back this up
    ├── webfs_share/             # Uploaded ISOs, kernels, initrds (served at /files/)
    │   ├── monitor/             # Files uploaded via the Lab Monitor dashboard
    │   └── <iso-name>/          # kernel + initrd auto-extracted from an uploaded ISO
    ├── ipxe_manager/            # Boot menu entries + autoinstall profiles (JSON)
    ├── ccp/                     # CCP SQLite db, job logs, uploaded files, SSH key
    ├── certs/                   # TLS cert + key for the optional HTTPS listener
    └── dnsmasq.leases           # Live DHCP lease database (read by monitor)
```

---

MIT License. See `LICENSE`.
