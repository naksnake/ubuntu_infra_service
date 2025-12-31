# SIT Services (Linux-only) — Webfs/HTTP, DHCP+iPXE, TFTP, Jenkins

A ready-to-run SIT environment focused on **Linux** only. It includes a fixed Webfs path for downloads, DHCP+iPXE, TFTP, Jenkins (persistent data), and **persistent NAT** options using **nftables** (recommended) or legacy iptables.

- **Fixed download path**: host `./data/webfs_share` → `http://<WEBFS_HOST_IP>:<WEBFS_PORT>/files/`
- **Linux-only iPXE**: UEFI‑friendly **kernel+initrd** flow, plus BIOS‑oriented ISO **sanboot** for quick tests.
- **DHCP/TFTP**: provided by dnsmasq and tftpd-hpa (containers run with `network_mode: host`).
- **Jenkins LTS**: persistent home at `./data/jenkins_home`.

> ⚠️ Make sure your PXE/LAB segment has **no other DHCP server**.

---


## Quickstart (One command)

```bash
git clone <repo-url>
cd ubuntu_infra_service
chmod +x deploy.sh
./deploy.sh

# Stop/Cleanup
#Stop services
docker compose down
#Persistent data is stored under ./data/* as bind mounts.
sudo rm -rf data/jenkins_home data/webfs_share
```

## Zero-to-Ready: From a Clean Server (Ubuntu/Debian)

### 1) Install Docker + Compose
```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose-plugin
sudo systemctl enable --now docker

docker --version && docker compose version
```

### 2) Identify NICs
Use `ip addr` (or `nmcli device status`) to find:
- `PXE_IFACE` → NIC connected to your PXE/LAB
- `WAN_IFACE` → NIC connected to the Internet (only if you need NAT)

### 3) Enable IPv4 forwarding (host)
```bash
echo "net.ipv4.ip_forward=1" | sudo tee /etc/sysctl.d/99-sit-nat.conf
sudo sysctl --system
```

### 4) Get the project and create required paths
```bash
unzip sit-services-linux-only-plus.zip
cd sit-services-linux-only-plus

# Required host paths
mkdir -p data/jenkins_home
mkdir -p data/webfs_share
mkdir -p services/webfs/htdocs/linux
```

### 5) Configure `.env`
Edit the following variables at minimum:
```ini
PXE_IFACE=eth1
WAN_IFACE=eth0
PXE_RANGE_START=192.168.100.10
PXE_RANGE_END=192.168.100.200
PXE_NETMASK=255.255.255.0
PXE_ROUTER_IP=192.168.100.1
WEBFS_HOST_IP=192.168.100.1
TFTP_SERVER_IP=192.168.100.1
DNS_SERVER=8.8.8.8
WEBFS_PORT=8080
JENKINS_HTTP_PORT=8081
JENKINS_AGENT_PORT=50000
ISO_FILE=ubuntu-live.iso   # only if testing BIOS ISO sanboot
```

### 6) Fetch iPXE binaries (BIOS/UEFI)
```bash
./scripts/get-ipxe-binaries.sh
ls services/tftp/tftpboot/
# expect: undionly.kpxe, ipxe.efi
```

### 7) Provide Linux boot assets
- **UEFI friendly (recommended):**
  ```bash
  cp vmlinuz services/webfs/htdocs/linux/
  cp initrd.img services/webfs/htdocs/linux/
  ```
  Adjust kernel parameters in `ipxe/linux-kernel-initrd.ipxe` (e.g., `root=`, `ip=dhcp`).

- **Optional BIOS ISO sanboot:**
  ```bash
  cp ubuntu-live.iso data/webfs_share/
  ```

### 8) Start services
```bash
docker compose up -d --build
```

### 9) Persistent NAT (if PXE/LAB needs Internet)
- **Recommended (nftables + systemd):**
  ```bash
  sudo ./scripts/persist-nat-nftables.sh
  sudo nft list ruleset | grep sit_nat
  sudo systemctl status sit-nat.service
  ```
- **Legacy (iptables-persistent):**
  ```bash
  sudo ./scripts/persist-nat-iptables.sh
  ```

### 10) Verify
```bash
./scripts/check-services.sh
# Webfs:   http://<WEBFS_HOST_IP>:8080/   (fixed share /files/)
# Jenkins: http://<host-ip>:8081/
```

---

## Linux Boot Flows (iPXE)

### A) Kernel + initrd (UEFI‑friendly)
Place kernel/initrd under `services/webfs/htdocs/linux/` and tune `ipxe/linux-kernel-initrd.ipxe`:
```ipxe
#!ipxe
set base http://${WEBFS_HOST_IP}:${WEBFS_PORT}
# Add your kernel parameters: root=, ip=dhcp, console=...
kernel ${base}/linux/vmlinuz initrd=initrd.magic ip=dhcp console=tty0
initrd ${base}/linux/initrd.img
boot
```
> For older kernels (< 5.7) on UEFI, `initrd=initrd.magic` ensures the injected initrd works.

### B) ISO sanboot (BIOS‑oriented quick test)
Put a Live ISO into `./data/webfs_share` and set `ISO_FILE` in `.env`. Use `ipxe/boot-iso.ipxe`.

---

## Project Structure
```
ubuntu_infra_service/
├─ deploy.sh
├─ .env.example
├─ .gitignore
├─ docker-compose.yml
├─ README.md                  # update quickstart section
├─ LICENSE
├─ CHANGELOG.md
├─ Jenkinsfile                # keep if you want; not required for deploy.sh
│
├─ ipxe/
│  ├─ default.ipxe
│  ├─ linux-kernel-initrd.ipxe
│  ├─ boot-iso.ipxe
│  └─ menu.ipxe
│
├─ services/
│  ├─ webfs/...
│  ├─ tftp/...
│  ├─ dhcp/...
│  └─ jenkins/...
│
├─ data/                      # runtime only (gitignored)
│  ├─ jenkins_home/
│  └─ webfs_share/
│
└─ .github/
   └─ workflows/
      └─ ci.yml

```

## Notes & Good Practices
- Target host: **Linux**. Docker Desktop (Win/Mac) may behave differently for `host` networking and DHCP.
- Avoid DHCP conflicts on the PXE/LAB segment.
- Consider pinning container base images for reproducibility.
- Back up `data/jenkins_home` and any critical files under `data/webfs_share`.

MIT License. See `LICENSE`.
