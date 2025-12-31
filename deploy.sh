#!/usr/bin/env bash
set -euo pipefail

# ==========================================================
# SIT Services deploy wizard (Linux-only)
# Usage:
#   ./deploy.sh
#
# What it does:
# - Ensures Docker + docker compose plugin
# - Creates required host dirs (data/*, linux assets path)
# - Interactive .env creation
# - Downloads iPXE binaries into services/tftp/tftpboot/
# - docker compose up -d --build
# - Optional persistent NAT (nftables recommended) via systemd unit
# - Basic verification (containers + webfs /files/ check)
# ==========================================================

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

GREEN="\033[0;32m"; YELLOW="\033[0;33m"; RED="\033[0;31m"; NC="\033[0m"
log()  { echo -e "${GREEN}[deploy]${NC} $*"; }
warn() { echo -e "${YELLOW}[deploy]${NC} $*" >&2; }
die()  { echo -e "${RED}[deploy] ERROR:${NC} $*" >&2; exit 1; }

have() { command -v "$1" >/dev/null 2>&1; }

sudo_run() {
  if [[ $EUID -eq 0 ]]; then
    "$@"
  else
    if ! have sudo; then
      die "sudo is required (or run as root)."
    fi
    sudo "$@"
  fi
}

detect_os_id() {
  if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    echo "${ID:-unknown}"
  else
    echo "unknown"
  fi
}

prompt() {
  local msg="$1" default="${2:-}"
  local ans=""
  if [[ -n "$default" ]]; then
    read -r -p "$msg [$default]: " ans
    echo "${ans:-$default}"
  else
    read -r -p "$msg: " ans
    echo "$ans"
  fi
}

prompt_yesno() {
  local msg="$1" default="${2:-N}"
  local ans=""
  read -r -p "$msg (y/N) " ans
  ans="${ans:-$default}"
  [[ "$ans" =~ ^[Yy]$ ]]
}

list_ifaces() {
  # exclude lo and typical virtual interfaces
  ls /sys/class/net | grep -vE '^(lo|docker|br-|veth|virbr|vmnet|zt|wg)' || true
}

choose_iface() {
  local title="$1"
  mapfile -t ifs < <(list_ifaces)
  [[ "${#ifs[@]}" -gt 0 ]] || die "No usable network interfaces found."

  log "$title"
  local i=1
  for n in "${ifs[@]}"; do
    echo "  [$i] $n"
    i=$((i+1))
  done

  local idx
  idx="$(prompt "Select number" "1")"
  [[ "$idx" =~ ^[0-9]+$ ]] || die "Invalid selection."
  (( idx >= 1 && idx <= ${#ifs[@]} )) || die "Invalid selection."
  echo "${ifs[$((idx-1))]}"
}

ensure_dirs() {
  # Your README requires these host paths. [1](https://github.com/naksnake/ubuntu_infra_service/blob/main/.env)
  log "Creating required directories..."
  mkdir -p data/jenkins_home
  mkdir -p data/webfs_share
  mkdir -p services/webfs/htdocs/linux
  mkdir -p services/tftp/tftpboot
}

ensure_docker() {
  if have docker && docker --version >/dev/null 2>&1; then
    log "Docker found: $(docker --version)"
  else
    if prompt_yesno "Docker not found. Install docker.io + docker-compose-plugin now?"; then
      local os
      os="$(detect_os_id)"
      case "$os" in
        ubuntu|debian)
          sudo_run apt-get update -y
          sudo_run apt-get install -y docker.io docker-compose-plugin curl ca-certificates
          sudo_run systemctl enable --now docker || true
          ;;
        *)
          die "Unsupported OS ($os). Please install Docker + Compose plugin manually."
          ;;
      esac
    else
      die "Docker is required."
    fi
  fi

  docker compose version >/dev/null 2>&1 || die "docker compose plugin missing. Install docker-compose-plugin."
  log "Compose OK: $(docker compose version)"
}

load_env() {
  if [[ -f .env ]]; then
    # shellcheck disable=SC1091
    set -a
    . ./.env
    set +a
  fi
}

write_env() {
  cat > .env <<EOF
# ==== Network settings ====
PXE_IFACE=${PXE_IFACE}
WAN_IFACE=${WAN_IFACE}
PXE_RANGE_START=${PXE_RANGE_START}
PXE_RANGE_END=${PXE_RANGE_END}
PXE_NETMASK=${PXE_NETMASK}
PXE_ROUTER_IP=${PXE_ROUTER_IP}
WEBFS_HOST_IP=${WEBFS_HOST_IP}
TFTP_SERVER_IP=${TFTP_SERVER_IP}
DNS_SERVER=${DNS_SERVER}

# ==== Ports ====
WEBFS_PORT=${WEBFS_PORT}
JENKINS_HTTP_PORT=${JENKINS_HTTP_PORT}
JENKINS_AGENT_PORT=${JENKINS_AGENT_PORT}

# Optional: ISO name for BIOS-only sanboot (Linux Live ISO)
ISO_FILE=${ISO_FILE}
EOF
  log "Wrote .env"
}

env_wizard() {
  log "=== Interactive configuration (.env) ==="
  warn "DHCP will run on PXE_IFACE. Ensure there is NO other DHCP server on that PXE/LAB segment." # [1](https://github.com/naksnake/ubuntu_infra_service/blob/main/.env)

  load_env

  PXE_IFACE="${PXE_IFACE:-}"
  WAN_IFACE="${WAN_IFACE:-}"

  if [[ -z "$PXE_IFACE" ]] || [[ ! -d "/sys/class/net/$PXE_IFACE" ]]; then
    PXE_IFACE="$(choose_iface "Select PXE/LAB interface (DHCP+TFTP will bind here)")"
  else
    if ! prompt_yesno "Use PXE_IFACE=$PXE_IFACE ?"; then
      PXE_IFACE="$(choose_iface "Select PXE/LAB interface (DHCP+TFTP will bind here)")"
    fi
  fi

  if [[ -z "$WAN_IFACE" ]] || [[ ! -d "/sys/class/net/$WAN_IFACE" ]]; then
    WAN_IFACE="$(choose_iface "Select WAN/Internet interface (needed only if NAT)")"
  else
    if ! prompt_yesno "Use WAN_IFACE=$WAN_IFACE ?"; then
      WAN_IFACE="$(choose_iface "Select WAN/Internet interface (needed only if NAT)")"
    fi
  fi

  [[ "$PXE_IFACE" != "$WAN_IFACE" ]] || die "PXE_IFACE and WAN_IFACE must be different."

  PXE_RANGE_START="$(prompt "PXE_RANGE_START" "${PXE_RANGE_START:-192.168.100.10}")"
  PXE_RANGE_END="$(prompt "PXE_RANGE_END" "${PXE_RANGE_END:-192.168.100.200}")"
  PXE_NETMASK="$(prompt "PXE_NETMASK" "${PXE_NETMASK:-255.255.255.0}")"
  PXE_ROUTER_IP="$(prompt "PXE_ROUTER_IP (gateway for PXE clients)" "${PXE_ROUTER_IP:-192.168.100.1}")"

  WEBFS_HOST_IP="$(prompt "WEBFS_HOST_IP (clients reach webfs here)" "${WEBFS_HOST_IP:-192.168.100.1}")"
  TFTP_SERVER_IP="$(prompt "TFTP_SERVER_IP" "${TFTP_SERVER_IP:-192.168.100.1}")"
  DNS_SERVER="$(prompt "DNS_SERVER" "${DNS_SERVER:-8.8.8.8}")"

  WEBFS_PORT="$(prompt "WEBFS_PORT" "${WEBFS_PORT:-8080}")"
  JENKINS_HTTP_PORT="$(prompt "JENKINS_HTTP_PORT" "${JENKINS_HTTP_PORT:-8081}")"
  JENKINS_AGENT_PORT="$(prompt "JENKINS_AGENT_PORT" "${JENKINS_AGENT_PORT:-50000}")"

  ISO_FILE="$(prompt "ISO_FILE (optional BIOS sanboot)" "${ISO_FILE:-example.iso}")"

  write_env
  load_env
}

fetch_ipxe_binaries() {
  # iPXE official docs recommend downloading these prebuilt binaries for chainloading. [3](https://ipxe.org/howto/chainloading)
  local dst="services/tftp/tftpboot"
  mkdir -p "$dst"

  if prompt_yesno "Download iPXE binaries into $dst now?"; then
    log "Downloading undionly.kpxe and ipxe.efi from boot.ipxe.org..."
    sudo_run apt-get update -y >/dev/null 2>&1 || true
    sudo_run apt-get install -y curl ca-certificates >/dev/null 2>&1 || true

    curl -fsSL "http://boot.ipxe.org/undionly.kpxe" -o "$dst/undionly.kpxe"
    curl -fsSL "http://boot.ipxe.org/ipxe.efi" -o "$dst/ipxe.efi"
    log "Downloaded: $dst/undionly.kpxe, $dst/ipxe.efi"
  else
    warn "Skipped iPXE binaries download."
  fi
}

compose_up() {
  log "Starting stack: docker compose up -d --build"
  docker compose up -d --build
  log "Stack started. (restart: unless-stopped is defined in compose.)" # [2](https://ipxe.org/download)
}

enable_ip_forwarding() {
  log "Enabling IPv4 forwarding permanently..."
  sudo_run bash -c 'echo "net.ipv4.ip_forward=1" > /etc/sysctl.d/99-sit-nat.conf'
  sudo_run sysctl --system >/dev/null
}

enable_nat_nftables() {
  # Use nftables masquerade in postrouting nat chain as recommended by nftables docs. [4](https://linuxvox.com/blog/linux-tftp-server-ubuntu/)
  enable_ip_forwarding
  sudo_run apt-get update -y >/dev/null 2>&1 || true
  sudo_run apt-get install -y nftables >/dev/null 2>&1 || true

  local unit="/etc/systemd/system/sit-nat.service"
  local script="/usr/local/sbin/sit-nat.sh"

  log "Creating NAT script: $script"
  sudo_run bash -c "cat > '$script' <<'EOS'
#!/usr/bin/env bash
set -euo pipefail

# Load env from repo if present (optional), but usually we embed env values at install time.
WAN_IFACE=\"${WAN_IFACE}\"
PXE_IFACE=\"${PXE_IFACE}\"

# Create dedicated table + chains
nft list table ip sit_nat >/dev/null 2>&1 || nft add table ip sit_nat

# nat postrouting with masquerade
nft list chain ip sit_nat postrouting >/dev/null 2>&1 || nft 'add chain ip sit_nat postrouting { type nat hook postrouting priority 100; }'

# forward chain for filtering (optional but useful)
nft list chain ip sit_nat forward >/dev/null 2>&1 || nft 'add chain ip sit_nat forward { type filter hook forward priority 0; policy accept; }'

# Flush previous rules (idempotent apply)
nft flush chain ip sit_nat postrouting || true
nft flush chain ip sit_nat forward || true

# Allow forwarding from PXE -> WAN and established back
nft add rule ip sit_nat forward iifname \"${PXE_IFACE}\" oifname \"${WAN_IFACE}\" accept
nft add rule ip sit_nat forward iifname \"${WAN_IFACE}\" oifname \"${PXE_IFACE}\" ct state established,related accept

# Masquerade outbound on WAN
nft add rule ip sit_nat postrouting oifname \"${WAN_IFACE}\" masquerade
EOS
chmod +x '$script'"

  log "Creating systemd unit: $unit"
  sudo_run bash -c "cat > '$unit' <<EOF
[Unit]
Description=SIT NAT (nftables) for PXE/LAB
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=$script
ExecReload=$script
ExecStop=/usr/sbin/nft delete table ip sit_nat

[Install]
WantedBy=multi-user.target
EOF"

  log "Enabling service..."
  sudo_run systemctl daemon-reload
  sudo_run systemctl enable --now sit-nat.service

  log "NAT enabled (nftables). Verify: sudo nft list ruleset | grep sit_nat" # [1](https://github.com/naksnake/ubuntu_infra_service/blob/main/.env)
}

enable_nat_iptables() {
  enable_ip_forwarding
  sudo_run apt-get update -y >/dev/null 2>&1 || true
  sudo_run apt-get install -y iptables iptables-persistent >/dev/null 2>&1 || true

  log "Applying iptables NAT (MASQUERADE) rules..."
  sudo_run iptables -t nat -A POSTROUTING -o "$WAN_IFACE" -j MASQUERADE
  sudo_run iptables -A FORWARD -i "$PXE_IFACE" -o "$WAN_IFACE" -j ACCEPT
  sudo_run iptables -A FORWARD -i "$WAN_IFACE" -o "$PXE_IFACE" -m state --state RELATED,ESTABLISHED -j ACCEPT

  log "Saving iptables rules (iptables-persistent)..."
  sudo_run netfilter-persistent save
  sudo_run systemctl enable --now netfilter-persistent
  log "NAT enabled (iptables-persistent)."
}

nat_wizard() {
  if prompt_yesno "Enable persistent NAT for PXE/LAB clients?"; then
    if prompt_yesno "Use recommended nftables method?"; then
      enable_nat_nftables
    else
      if prompt_yesno "Use iptables-persistent fallback?"; then
        enable_nat_iptables
      else
        warn "NAT skipped."
      fi
    fi
  else
    log "NAT not enabled."
  fi
}

check_stack() {
  log "=== Status ==="
  docker ps || true

  # Smoke-check webfs fixed share (/files/) which is central to your repo design. [1](https://github.com/naksnake/ubuntu_infra_service/blob/main/.env)[2](https://ipxe.org/download)
  if have curl; then
    local url="http://127.0.0.1:${WEBFS_PORT:-8080}/files/"
    log "Checking Webfs /files/ is reachable: $url"
    curl -fsSI "$url" >/dev/null || warn "Webfs /files/ not reachable yet (may take a few seconds)."
  else
    warn "curl not installed; skipping HTTP checks."
  fi
}

main() {
  log "SIT deploy wizard starting..."
  ensure_docker
  ensure_dirs

  # Create .env from example if not present
  if [[ ! -f .env ]]; then
    if [[ -f .env.example ]]; then
      log "No .env found. Copying .env.example -> .env"
      cp .env.example .env
    else
      warn "No .env.example found; will create .env via wizard."
    fi
  fi

  load_env
  if prompt_yesno "Run interactive configuration wizard now?" "Y"; then
    env_wizard
  else
    log "Using existing .env as-is."
  fi

  fetch_ipxe_binaries
  compose_up
  nat_wizard
  check_stack

  echo
  log "DONE."
  echo "Webfs:   http://${WEBFS_HOST_IP:-<WEBFS_HOST_IP>}:${WEBFS_PORT:-8080}/   (fixed share /files/)"  # [1](https://github.com/naksnake/ubuntu_infra_service/blob/main/.env)
  echo "Jenkins: http://<host-ip>:${JENKINS_HTTP_PORT:-8081}/"                                          # [1](https://github.com/naksnake/ubuntu_infra_service/blob/main/.env)
  echo
  warn "Reminder: DHCP is running on PXE_IFACE=${PXE_IFACE}. Ensure no other DHCP server exists on that lab segment." # [1](https://github.com/naksnake/ubuntu_infra_service/blob/main/.env)
}

main "$@"

