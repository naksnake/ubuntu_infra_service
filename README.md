# Lab Services — PXE lab-in-a-box

A single-command PXE lab environment for Ubuntu/Debian hosts.  
Run `./deploy.sh` once — DHCP, TFTP, NAT, an HTTP file server, a web-based
iPXE boot-menu manager, a health dashboard, and Ansible AWX all start
automatically and survive reboots.

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
cp .env.example .env          # edit: PXE_IFACE, WAN_IFACE, AWX_ADMIN_PASSWORD

# 3. Deploy (answer yes to the NAT and autostart prompts):
./deploy.sh
```

Then open `http://192.168.100.1:8091/` (iPXE Manager) to upload an ISO and
build your boot menu, and `http://192.168.100.1:8090/` (Monitor) to watch
service health and DHCP leases. Full details in the steps below.

---

## What you get

| Service | Container | Purpose |
|---|---|---|
| DHCP + PXE | `lab_dhcp` | Assigns IPs to lab clients, serves iPXE bootloaders |
| TFTP | `lab_tftp` | Delivers bootloader files to PXE clients |
| File server | `lab_webfs` | HTTP share for ISO images, kernels, initrds |
| iPXE Manager | `lab_ipxe_manager` | Web UI: upload boot files, edit the PXE boot menu |
| Ansible AWX | `lab_awx_web` + `lab_awx_task` | Web UI for running Ansible playbooks |
| AWX database | `lab_awx_postgres` | PostgreSQL for AWX |
| AWX cache | `lab_awx_redis` | Redis for AWX |
| NAT | systemd `lab-nat` | Lets lab clients reach the internet via the host |
| Monitor | `lab_monitor` | Web dashboard: service health + DHCP lease lookup |

---

## Requirements

- **OS**: Ubuntu 22.04 / 24.04 / 26.04 (Server **or** Desktop) or Debian 12
  (Linux only — Docker Desktop on Mac/Windows does not support host networking)
- **NICs**: Two network interfaces
  - `PXE_IFACE` — connected to your lab switch (DHCP + TFTP will bind here)
  - `WAN_IFACE` — connected to the internet (used for NAT)
- **CPU / RAM**: 4 cores and 8 GB RAM minimum (AWX uses ~3–4 GB); a dual-LAN
  mini PC like an Intel N97/N100 box with 16 GB is a comfortable fit
- **Root / sudo**: required for Docker install, IP forwarding, NAT setup, and systemd unit
- **Disk**: ~5 GB free for the stack (AWX image is ~1.5 GB) plus space for
  your ISO images in `data/webfs_share/`

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
AWX_HTTP_PORT=8052
MONITOR_PORT=8090

# ---- iPXE Manager (optional) ----
# Set a password to require login for the manager UI/API.
# Leave blank for no auth. /menu.ipxe always stays open for PXE clients.
IPXE_MANAGER_PASSWORD=

# ---- AWX ----
AWX_VERSION=23.9.0
AWX_ADMIN_USER=admin
AWX_ADMIN_PASSWORD=YourPassword123   # at least 8 characters

# AWX_DB_PASSWORD and AWX_SECRET_KEY are auto-generated by deploy.sh.
# Leave them blank — deploy.sh fills them in.
AWX_DB_PASSWORD=
AWX_SECRET_KEY=
```

---

## Step 4 — Run the deploy wizard

```bash
./deploy.sh
```

The wizard will ask you to confirm each setting, then it will:

1. Install Docker + Compose plugin (if not present)
2. Create the required data directories
3. Auto-generate `AWX_DB_PASSWORD` and `AWX_SECRET_KEY` and save them to `.env`
4. Render `services/awx/credentials.py` from the template
5. Offer to download iPXE boot binaries (`undionly.kpxe`, `ipxe.efi`)
6. Build and start all containers with `docker compose up -d --build`
7. Offer to enable persistent NAT via a systemd unit (`lab-nat.service`)
8. Offer to enable stack autostart on reboot via `lab-stack.service`

**Answer yes to both the NAT and autostart prompts** to get a fully persistent lab.

> **AWX first-boot**: AWX runs database migrations on its first start. The UI will not be available for **3–5 minutes** after the containers start. This is normal.

---

## Step 5 — Verify everything is running

### Check containers
```bash
docker ps
```
Expected output — all nine containers should show `Up`:
```
CONTAINER ID   IMAGE                          STATUS
...            lab_ipxe_manager               Up X minutes (healthy)   lab_ipxe_manager
...            lab_monitor                    Up X minutes (healthy)   lab_monitor
...            ghcr.io/ansible/awx:23.9.0    Up X minutes (healthy)   lab_awx_web
...            ghcr.io/ansible/awx:23.9.0    Up X minutes             lab_awx_task
...            postgres:15-alpine             Up X minutes (healthy)   lab_awx_postgres
...            redis:7-alpine                 Up X minutes (healthy)   lab_awx_redis
...            lab_webfs                      Up X minutes (healthy)   lab_webfs
...            lab_dhcp                       Up X minutes (healthy)   lab_dhcp
...            lab_tftp                       Up X minutes (healthy)   lab_tftp
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
sudo nft list ruleset | grep lab_nat
```

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

### Verify AWX
Open a browser and go to:
```
http://192.168.100.1:8052/
```
Log in with `AWX_ADMIN_USER` / `AWX_ADMIN_PASSWORD` from your `.env`.

If AWX is not ready yet, watch it start:
```bash
docker logs -f lab_awx_web
# Wait for "supervisord started" in the output
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
(PXE clients can always fetch `/menu.ipxe` without a password).

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
docker logs -f lab_awx_web
docker logs -f lab_awx_task

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
| AWX (web + task + postgres + redis) | ~3–4 GB |
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
- Check nftables rules: `sudo nft list ruleset | grep lab_nat`
- Restart NAT service: `sudo systemctl restart lab-nat.service`

**AWX UI shows 502 / not reachable after 5 minutes**
- Check all AWX containers are running: `docker ps | grep awx`
- Check postgres is healthy: `docker inspect lab_awx_postgres | grep Health -A5`
- Check AWX logs: `docker logs lab_awx_web 2>&1 | tail -30`

**Containers restart repeatedly**
- Check for missing `.env` values: `docker logs lab_dhcp | head -5`
- Confirm `services/awx/credentials.py` exists: `ls -la services/awx/`  
  If missing, re-run: `./deploy.sh` (it will skip the config wizard if `.env` exists)

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
│   ├── ipxe_manager/            # Web UI: file uploads + PXE boot menu editor
│   ├── monitor/                 # Flask dashboard (service health + DHCP leases)
│   └── awx/
│       ├── credentials.py.template   # AWX DB+Redis config (rendered by deploy.sh)
│       └── environment.sh            # AWX admin user init helper
│
└── data/                        # Runtime data — back this up
    ├── awx_postgres/            # AWX database files
    ├── awx_projects/            # Ansible playbook directories (mount into AWX)
    ├── webfs_share/             # Uploaded ISOs, kernels, initrds (served at /files/)
    ├── ipxe_manager/            # Boot menu entries (entries.json)
    └── dnsmasq.leases           # Live DHCP lease database (read by monitor)
```

---

MIT License. See `LICENSE`.
