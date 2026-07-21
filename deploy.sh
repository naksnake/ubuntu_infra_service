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
# - Interactive .env creation with auto-generated secrets
# - Downloads iPXE binaries into services/tftp/tftpboot/
# - Renders services/awx/credentials.py from template
# - docker compose up -d --build
# - Optional persistent NAT (nftables recommended) via systemd unit
# - Optional systemd autostart (lab-stack.service)
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
    have sudo || die "sudo is required (or run as root)."
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

gen_secret() {
  # Generate a URL-safe random secret; falls back to /dev/urandom if openssl unavailable.
  if have openssl; then
    openssl rand -base64 48 | tr -d '\n/+=' | head -c 50
  else
    tr -dc 'A-Za-z0-9' </dev/urandom | head -c 50
  fi
}

ensure_dirs() {
  log "Creating required directories..."
  mkdir -p data/awx_postgres
  mkdir -p data/awx_projects
  mkdir -p data/webfs_share
  mkdir -p services/webfs/htdocs/linux
  mkdir -p services/tftp/tftpboot
  mkdir -p services/awx
  # dnsmasq writes leases here; must exist as a file before Docker bind-mounts it
  touch data/dnsmasq.leases
  # iPXE Manager state (entries.json lives inside; directory mount keeps
  # the manager's atomic tmp+rename writes working)
  mkdir -p data/ipxe_manager
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
IPXE_MANAGER_PORT=${IPXE_MANAGER_PORT}
AWX_HTTP_PORT=${AWX_HTTP_PORT}
MONITOR_PORT=${MONITOR_PORT}
MONITOR_REFRESH=${MONITOR_REFRESH:-30}

# ==== iPXE Manager ====
# Optional: set a password to protect the web UI and API (menu.ipxe stays open).
# Quoted so a password containing spaces survives sourcing and compose parsing.
IPXE_MANAGER_PASSWORD="${IPXE_MANAGER_PASSWORD:-}"

# ==== AWX settings ====
AWX_VERSION=${AWX_VERSION}
AWX_ADMIN_USER=${AWX_ADMIN_USER}
AWX_ADMIN_PASSWORD="${AWX_ADMIN_PASSWORD}"
AWX_DB_PASSWORD="${AWX_DB_PASSWORD}"
AWX_SECRET_KEY="${AWX_SECRET_KEY}"

# Optional: ISO name for BIOS-only sanboot (Linux Live ISO)
ISO_FILE=${ISO_FILE}
EOF
  log "Wrote .env"
}

env_wizard() {
  log "=== Interactive configuration (.env) ==="
  warn "DHCP will run on PXE_IFACE. Ensure there is NO other DHCP server on that PXE/LAB segment."

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
  IPXE_MANAGER_PORT="$(prompt "IPXE_MANAGER_PORT" "${IPXE_MANAGER_PORT:-8091}")"
  AWX_HTTP_PORT="$(prompt "AWX_HTTP_PORT" "${AWX_HTTP_PORT:-8052}")"
  MONITOR_PORT="$(prompt "MONITOR_PORT" "${MONITOR_PORT:-8090}")"

  # AWX version
  AWX_VERSION="$(prompt "AWX_VERSION (see github.com/ansible/awx/releases)" "${AWX_VERSION:-23.9.0}")"

  # AWX admin credentials
  AWX_ADMIN_USER="$(prompt "AWX_ADMIN_USER" "${AWX_ADMIN_USER:-admin}")"
  while true; do
    AWX_ADMIN_PASSWORD="$(prompt "AWX_ADMIN_PASSWORD (min 8 chars)")"
    [[ "${#AWX_ADMIN_PASSWORD}" -ge 8 ]] && break
    warn "Password must be at least 8 characters."
  done

  # Auto-generate secrets if not already set
  if [[ -z "${AWX_DB_PASSWORD:-}" ]]; then
    AWX_DB_PASSWORD="$(gen_secret)"
    log "Generated AWX_DB_PASSWORD."
  fi
  if [[ -z "${AWX_SECRET_KEY:-}" ]]; then
    AWX_SECRET_KEY="$(gen_secret)"
    log "Generated AWX_SECRET_KEY."
  fi

  ISO_FILE="$(prompt "ISO_FILE (optional BIOS sanboot)" "${ISO_FILE:-example.iso}")"

  write_env
  load_env
}

render_awx_credentials() {
  local template="services/awx/credentials.py.template"
  local output="services/awx/credentials.py"

  [[ -f "$template" ]] || die "AWX credentials template not found: $template"

  log "Rendering $output from template..."
  # shellcheck disable=SC2016
  AWX_DB_PASSWORD="${AWX_DB_PASSWORD}" envsubst '${AWX_DB_PASSWORD}' < "$template" > "$output"
  log "Rendered $output"
}

fetch_ipxe_binaries() {
  local dst="services/tftp/tftpboot"
  mkdir -p "$dst"

  if prompt_yesno "Download iPXE binaries (undionly.kpxe, ipxe.efi) into $dst now?"; then
    log "Downloading from boot.ipxe.org..."
    sudo_run apt-get update -y >/dev/null 2>&1 || true
    sudo_run apt-get install -y curl ca-certificates >/dev/null 2>&1 || true

    curl -fsSL "https://boot.ipxe.org/undionly.kpxe" -o "$dst/undionly.kpxe"
    curl -fsSL "https://boot.ipxe.org/ipxe.efi"     -o "$dst/ipxe.efi"
    log "Downloaded: $dst/undionly.kpxe, $dst/ipxe.efi"
  else
    warn "Skipped iPXE binaries download."
  fi
}

compose_up() {
  log "Starting stack: docker compose up -d --build"
  docker compose up -d --build
  log "Stack started. (restart: unless-stopped is set on all services)"
}

enable_ip_forwarding() {
  log "Enabling IPv4 forwarding permanently..."
  sudo_run bash -c 'echo "net.ipv4.ip_forward=1" > /etc/sysctl.d/99-lab-nat.conf'
  sudo_run sysctl --system >/dev/null
}

enable_nat_nftables() {
  enable_ip_forwarding
  sudo_run apt-get update -y >/dev/null 2>&1 || true
  sudo_run apt-get install -y nftables >/dev/null 2>&1 || true

  local unit="/etc/systemd/system/lab-nat.service"
  local script="/usr/local/sbin/lab-nat.sh"

  log "Creating NAT script: $script"
  sudo_run bash -c "cat > '$script' <<'EOS'
#!/usr/bin/env bash
set -euo pipefail
WAN_IFACE=\"${WAN_IFACE}\"
PXE_IFACE=\"${PXE_IFACE}\"

nft list table ip lab_nat >/dev/null 2>&1 || nft add table ip lab_nat
nft list chain ip lab_nat postrouting >/dev/null 2>&1 || nft 'add chain ip lab_nat postrouting { type nat hook postrouting priority 100; }'
nft list chain ip lab_nat forward >/dev/null 2>&1     || nft 'add chain ip lab_nat forward { type filter hook forward priority 0; policy accept; }'

nft flush chain ip lab_nat postrouting || true
nft flush chain ip lab_nat forward     || true

nft add rule ip lab_nat forward iifname \"${PXE_IFACE}\" oifname \"${WAN_IFACE}\" accept
nft add rule ip lab_nat forward iifname \"${WAN_IFACE}\" oifname \"${PXE_IFACE}\" ct state established,related accept
nft add rule ip lab_nat postrouting oifname \"${WAN_IFACE}\" masquerade
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
ExecStop=/usr/sbin/nft delete table ip lab_nat

[Install]
WantedBy=multi-user.target
EOF"

  sudo_run systemctl daemon-reload
  sudo_run systemctl enable --now lab-nat.service
  log "NAT enabled (nftables). Verify: sudo nft list ruleset | grep lab_nat"
}

enable_nat_iptables() {
  enable_ip_forwarding
  sudo_run apt-get update -y >/dev/null 2>&1 || true
  sudo_run apt-get install -y iptables iptables-persistent >/dev/null 2>&1 || true

  log "Applying iptables NAT (MASQUERADE) rules..."
  sudo_run iptables -t nat -A POSTROUTING -o "$WAN_IFACE" -j MASQUERADE
  sudo_run iptables -A FORWARD -i "$PXE_IFACE" -o "$WAN_IFACE" -j ACCEPT
  sudo_run iptables -A FORWARD -i "$WAN_IFACE" -o "$PXE_IFACE" -m state --state RELATED,ESTABLISHED -j ACCEPT

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

enable_autostart() {
  local unit="/etc/systemd/system/lab-stack.service"
  local repo_dir="$ROOT_DIR"

  log "Creating systemd autostart unit: $unit"
  sudo_run bash -c "cat > '$unit' <<EOF
[Unit]
Description=SIT Lab Stack (docker compose)
After=docker.service network-online.target
Requires=docker.service
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=${repo_dir}
ExecStart=/usr/bin/docker compose up -d --remove-orphans
ExecStop=/usr/bin/docker compose down
ExecReload=/usr/bin/docker compose up -d --remove-orphans
TimeoutStartSec=300

[Install]
WantedBy=multi-user.target
EOF"

  sudo_run systemctl daemon-reload
  sudo_run systemctl enable lab-stack.service
  log "lab-stack.service enabled — stack will auto-start after reboot."
}

check_stack() {
  log "=== Status ==="
  docker ps || true

  if have curl; then
    local url="http://127.0.0.1:${WEBFS_PORT:-8080}/files/"
    log "Checking Webfs /files/ : $url"
    curl -fsSI "$url" >/dev/null 2>&1 && log "Webfs OK" || warn "Webfs not reachable yet (may take a few seconds)."
  fi
}

main() {
  log "SIT deploy wizard starting..."
  ensure_docker
  ensure_dirs

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
    load_env
    # Ensure secrets exist even if wizard was skipped
    if [[ -z "${AWX_DB_PASSWORD:-}" ]]; then
      AWX_DB_PASSWORD="$(gen_secret)"
      warn "AWX_DB_PASSWORD was empty — generated new value."
      # Patch in-place atomically
      local tmp; tmp="$(mktemp .env.XXXXXX)"
      if grep -q '^AWX_DB_PASSWORD=' .env; then
        sed "s|^AWX_DB_PASSWORD=.*|AWX_DB_PASSWORD=${AWX_DB_PASSWORD}|" .env > "$tmp"
      else
        { cat .env; echo "AWX_DB_PASSWORD=${AWX_DB_PASSWORD}"; } > "$tmp"
      fi
      mv "$tmp" .env
    fi
    if [[ -z "${AWX_SECRET_KEY:-}" ]]; then
      AWX_SECRET_KEY="$(gen_secret)"
      warn "AWX_SECRET_KEY was empty — generated new value."
      local tmp; tmp="$(mktemp .env.XXXXXX)"
      if grep -q '^AWX_SECRET_KEY=' .env; then
        sed "s|^AWX_SECRET_KEY=.*|AWX_SECRET_KEY=${AWX_SECRET_KEY}|" .env > "$tmp"
      else
        { cat .env; echo "AWX_SECRET_KEY=${AWX_SECRET_KEY}"; } > "$tmp"
      fi
      mv "$tmp" .env
    fi
    load_env
    log "Using existing .env as-is."
  fi

  render_awx_credentials
  fetch_ipxe_binaries
  compose_up
  nat_wizard

  if prompt_yesno "Enable autostart on boot (systemd lab-stack.service)?"; then
    enable_autostart
  else
    log "Autostart skipped. Run 'sudo systemctl enable lab-stack.service' later if needed."
  fi

  check_stack

  echo
  log "DONE."
  echo "Webfs:        http://${WEBFS_HOST_IP:-<host>}:${WEBFS_PORT:-8080}/"
  echo "iPXE Manager: http://${WEBFS_HOST_IP:-<host>}:${IPXE_MANAGER_PORT:-8091}/   (upload files, manage boot menu)"
  echo "Monitor:      http://${WEBFS_HOST_IP:-<host>}:${MONITOR_PORT:-8090}/   (service health + DHCP leases)"
  echo "AWX:          http://${WEBFS_HOST_IP:-<host>}:${AWX_HTTP_PORT:-8052}/   (admin: ${AWX_ADMIN_USER:-admin})"
  echo
  warn "AWX first boot runs DB migrations — allow ~2 minutes before the UI is ready."
  warn "Reminder: DHCP is running on PXE_IFACE=${PXE_IFACE:-<PXE_IFACE>}. Ensure no other DHCP server exists on that lab segment."
}

main "$@"
