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
     ipv4.method manual ipv4.addresses 192.168.100.1/24
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
| Cluster Control Panel | `lab_ccp` | Web UI: run ClusterShell commands + Ansible playbooks across nodes, with login/RBAC, job history, script repo, and audit log |
| NAT | systemd `lab-nat` | Lets lab clients reach the internet via the host |
| Monitor | `lab_monitor` | Web dashboard: service health + DHCP lease lookup |

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
     ipv4.method manual ipv4.addresses 192.168.100.1/24
sudo nmcli con up lab-pxe
ip addr show enp2s0             # confirm 192.168.100.1 is shown
```
This survives reboots. Leave the WAN port on its normal DHCP connection.

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
PXE_ROUTER_IP=192.168.100.1      # host's PXE_IFACE IP (from Step 1)
WEBFS_HOST_IP=192.168.100.1      # same as PXE_ROUTER_IP
TFTP_SERVER_IP=192.168.100.1     # same as PXE_ROUTER_IP
DNS_SERVER=8.8.8.8

# ---- Ports ----
WEBFS_PORT=8080
IPXE_MANAGER_PORT=8091
CCP_PORT=8060
MONITOR_PORT=8090
MONITOR_REFRESH=30               # dashboard auto-refresh interval (seconds)

# ---- iPXE Manager (optional) ----
# Set a password to require login for the manager UI/API.
# Leave blank for no auth. /menu.ipxe and /autoinstall/ always stay open for PXE clients.
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
4. Offer to download iPXE boot binaries (`undionly.kpxe`, `ipxe.efi`) — defaults to **yes**
5. Build and start all containers with `docker compose up -d --build`
6. Offer to enable persistent NAT via a systemd unit (`lab-nat.service`)
7. Offer to enable stack autostart on reboot via `lab-stack.service`

**Answer yes to both the NAT and autostart prompts** to get a fully persistent lab.

> The Control Panel is ready within a few seconds of the containers starting —
> log in with `CCP_ADMIN_USER` / `CCP_ADMIN_PASSWORD` from your `.env`.

---

## Step 5 — Verify everything is running

### Check containers
```bash
docker ps
```
Expected output — all six containers should show `Up`:
```
CONTAINER ID   IMAGE                STATUS                     NAMES
...            lab_ipxe_manager     Up X minutes (healthy)     lab_ipxe_manager
...            lab_ccp              Up X minutes (healthy)     lab_ccp
...            lab_monitor          Up X minutes (healthy)     lab_monitor
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
You will see:
- **Services** — all container names, status (running/exited), health check result, uptime, restart count
- **DHCP Leases** — IP address, MAC address, hostname, lease expiry, and time remaining for every active lease

Use the search box to quickly find a host by IP, MAC, or hostname.  
The page auto-refreshes every 30 seconds. A JSON API is also available at `/api/status`.

### Verify the Cluster Control Panel
Open a browser and go to:
```
http://192.168.100.1:8060/
```
Log in with `CCP_ADMIN_USER` / `CCP_ADMIN_PASSWORD` from your `.env`. From here you can
add nodes, run ClusterShell commands and Ansible playbooks across them, keep a script
repository, and review job history and the audit log. See
[Cluster Control Panel](#cluster-control-panel-clustershell--ansible) below.

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
2. **Boot Menu tab** — click **+ Add Entry**, give it a name, pick the boot type:
   - **Kernel + initrd** — fetched over HTTP; works on **BIOS and UEFI**
     (this is the modern, recommended path)
   - **ISO sanboot** — **BIOS firmware only** (UEFI cannot sanboot an ISO)
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

> **ISO shortcut:** uploading a `.iso` still auto-creates a **disabled**
> sanboot entry (handy for BIOS clients or quick tests). For UEFI, use the
> Kernel + initrd type as above. Kernels are never auto-added because they
> need a matching initrd and command line.

### Boot order

The order in the Boot Menu tab is the order clients see, and the top enabled
entry (badged **default**) boots automatically after a 30-second timeout.
Use the ▲▼ arrows to change it; disabled entries are hidden from clients.

Every change is live immediately — the next PXE boot picks it up with no
container restart. You can rename, enable/disable, or delete entries at any
time; the **iPXE Preview** tab shows the exact script clients receive.

> You can also copy files straight into `data/webfs_share/` from the shell —
> they appear in the manager's file list and dropdowns automatically.

To password-protect the manager, set `IPXE_MANAGER_PASSWORD` in `.env`
(PXE clients can always fetch `/menu.ipxe` and `/autoinstall/…` without a password).

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

1. **Files tab** — upload the Ubuntu **live-server** ISO (plus its `vmlinuz` and
   `initrd` if you extracted them, or reference the ISO over HTTP).
2. **Autoinstall tab** → **+ New Profile**. The editor is pre-filled with a
   standard Ubuntu autoinstall template (identity, storage `layout: direct`, SSH
   server). Edit the hostname, user, password hash (`mkpasswd -m sha-512`), disk
   layout, and packages. It's validated as YAML on save.
3. **Boot Menu tab** → add or edit a **Kernel + initrd** entry. Point its command
   line at the matching ISO/rootfs, e.g.
   `ip=dhcp url=http://192.168.100.1:8080/files/ubuntu-24.04.1-live-server-amd64.iso`,
   then pick your profile in **Autoinstall profile**. The live preview shows the
   exact kernel line, including the appended seed URL.
4. Enable the entry only when you're ready; the next PXE boot of that machine
   installs Ubuntu unattended per your profile.

The **iPXE Preview** tab always shows the exact script clients receive.

---

## Cluster Control Panel (ClusterShell + Ansible)

Open the Control Panel:
```
http://192.168.100.1:8060/
```
Log in with `CCP_ADMIN_USER` / `CCP_ADMIN_PASSWORD`. It provides:

- **Login + RBAC** — three roles: `viewer` (read-only), `operator` (run jobs,
  manage nodes/scripts/files), `admin` (everything + user management + audit log).
- **Node management** — register hosts (SSH) or the control host itself (local).
  Group nodes with tags to target a whole group at once.
- **ClusterShell** — run a shell command across selected nodes/groups in parallel
  and see per-node output live.
- **Ansible** — run a playbook against selected nodes; an inventory is generated
  automatically. Output streams into the job log.
- **Script repository** — save reusable shell snippets and playbooks and load them
  into the ClusterShell/Ansible runners.
- **Job history** — every run is recorded with status, exit code, and full output.
- **Files** — upload/download shared files (kickstart snippets, tarballs, etc.).
- **Audit log** — every login and state-changing action is recorded (admin-only).

> **Try it without any real hosts:** set `CCP_DEMO=1` in `.env` before the first
> run to seed a self-contained `localhost` node plus sample scripts, so you can
> exercise ClusterShell and Ansible immediately.

SSH to real nodes uses the key at `data/ccp/ssh/id_ccp` (drop your private key
there, or generate one and distribute the matching public key to your nodes).

---

## Changing the DHCP range

You do not need to restart the full stack — only the DHCP container is recycled:

```bash
# With arguments:
./update-dhcp-range.sh 192.168.100.50 192.168.100.150

# Interactive:
./update-dhcp-range.sh
```

Existing leases are not affected until they expire.

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

---

## Autostart and NAT after reboot

After running `deploy.sh` with autostart and NAT enabled:

```bash
# Check autostart is enabled
sudo systemctl status lab-stack.service

# Check NAT is enabled
sudo systemctl status lab-nat.service
```

Both units start automatically at boot. To enable them manually if you skipped the prompts:
```bash
sudo systemctl enable --now lab-stack.service
sudo systemctl enable --now lab-nat.service
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

## Troubleshooting

**DHCP clients get no IP**
- Confirm `PXE_IFACE` has the static IP: `ip addr show <PXE_IFACE>`
- Check dnsmasq started: `docker logs lab_dhcp | head -20`
- Confirm no other DHCP server is on the lab segment: `sudo nmap --script broadcast-dhcp-discover`

**NAT not working (clients can ping gateway but not internet)**
- Check IP forwarding is on: `cat /proc/sys/net/ipv4/ip_forward` (must be `1`)
- Check the ACCEPT rules: `sudo iptables -S DOCKER-USER | grep -E "$PXE_IFACE|$WAN_IFACE"`
- Re-apply / restart NAT service: `sudo systemctl restart lab-nat.service`

**Cluster Control Panel won't load or log in fails**
- Check the container is up: `docker ps | grep lab_ccp`
- Check its logs: `docker logs lab_ccp 2>&1 | tail -30`
- Confirm `CCP_ADMIN_PASSWORD` is set in `.env` (the admin is seeded on first run only)

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
│   ├── linux-kernel-initrd.ipxe # UEFI-friendly kernel+initrd boot
│   └── boot-iso.ipxe            # BIOS-only ISO sanboot
│
├── services/
│   ├── dhcp/                    # dnsmasq DHCP container (PXE pointers, no TFTP)
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
    ├── ipxe_manager/            # Boot menu entries + autoinstall profiles (JSON)
    ├── ccp/                     # CCP SQLite db, job logs, uploaded files, SSH key
    └── dnsmasq.leases           # Live DHCP lease database (read by monitor)
```

---

MIT License. See `LICENSE`.
