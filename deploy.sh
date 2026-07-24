#!/usr/bin/env bash
set -euo pipefail

# ==========================================================
# Lab Services deploy wizard (Linux-only)
# Usage:
#   ./deploy.sh
#
# What it does:
# - Ensures Docker + docker compose plugin
# - Creates required host dirs (data/*, linux assets path)
# - Interactive .env creation with auto-generated secrets
# - Downloads iPXE binaries into services/tftp/tftpboot/
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

# Run docker as the current user if the daemon is reachable, else via sudo.
# (A freshly-installed docker.io often isn't usable by a non-root user until
# they re-login for docker-group membership; sudo bridges that gap.)
docker_cli() {
  if docker info >/dev/null 2>&1; then
    docker "$@"
  else
    sudo_run docker "$@"
  fi
}

# IPv4 dotted-quad validation (each octet 0-255).
valid_ipv4() {
  local ip="$1" o1 o2 o3 o4
  [[ "$ip" =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]] || return 1
  IFS='.' read -r o1 o2 o3 o4 <<< "$ip"
  for o in "$o1" "$o2" "$o3" "$o4"; do (( o >= 0 && o <= 255 )) || return 1; done
  return 0
}

prompt_ip() {
  local msg="$1" def="$2" ans
  while true; do
    ans="$(prompt "$msg" "$def")"
    valid_ipv4 "$ans" && { echo "$ans"; return; }
    warn "Not a valid IPv4 address: $ans"
  done
}

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
  local msg="$1" default="${2:-N}" hint ans
  if [[ "$default" =~ ^[Yy]$ ]]; then hint="(Y/n)"; else hint="(y/N)"; fi
  read -r -p "$msg $hint " ans
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

  # menu goes to stderr — stdout is captured as this function's return value
  printf '%b\n' "${GREEN}[deploy]${NC} $title" >&2
  local i=1
  for n in "${ifs[@]}"; do
    echo "  [$i] $n" >&2
    i=$((i+1))
  done

  local idx
  idx="$(prompt "Select number" "1")"
  [[ "$idx" =~ ^[0-9]+$ ]] || die "Invalid selection."
  (( idx >= 1 && idx <= ${#ifs[@]} )) || die "Invalid selection."
  echo "${ifs[$((idx-1))]}"
}

gen_secret() {
  # URL-safe random secret. The subshell disables pipefail so `head` closing the
  # pipe early does not SIGPIPE-kill the upstream command (which would abort the
  # whole script under `set -o pipefail`).
  ( set +o pipefail
    if have openssl; then
      openssl rand -base64 48 | tr -d '\n/+=' | head -c 50
    else
      LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 50
    fi )
}

# Ensure a generated secret exists in .env for the named key; patches in place.
ensure_env_secret() {
  local name="$1" val="${!1:-}"
  [[ -n "$val" ]] && return 0
  val="$(gen_secret)"
  printf -v "$name" '%s' "$val"
  warn "$name was empty — generated new value."
  local tmp; tmp="$(mktemp .env.XXXXXX)"
  if grep -q "^${name}=" .env; then
    sed "s|^${name}=.*|${name}=\"${val}\"|" .env > "$tmp"
  else
    # guarantee a trailing newline before appending
    cp .env "$tmp"
    [[ -s "$tmp" && -z "$(tail -c1 "$tmp")" ]] || echo >> "$tmp"
    echo "${name}=\"${val}\"" >> "$tmp"
  fi
  mv "$tmp" .env
}

ensure_dirs() {
  log "Creating required directories..."
  mkdir -p data/ccp
  mkdir -p data/webfs_share
  mkdir -p services/webfs/htdocs/linux
  mkdir -p services/tftp/tftpboot
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
          sudo_run apt-get install -y docker.io curl ca-certificates
          # The compose v2 plugin package is named differently across releases
          # (docker-compose-v2 on newer Ubuntu, docker-compose-plugin elsewhere);
          # try both, then verify `docker compose` works just below.
          sudo_run apt-get install -y docker-compose-v2 2>/dev/null \
            || sudo_run apt-get install -y docker-compose-plugin 2>/dev/null \
            || warn "No compose plugin package installed via apt — will verify 'docker compose' next."
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
PXE_LEASE_TIME=${PXE_LEASE_TIME:-12h}
PXE_ROUTER_IP=${PXE_ROUTER_IP}
WEBFS_HOST_IP=${WEBFS_HOST_IP}
TFTP_SERVER_IP=${TFTP_SERVER_IP}
DNS_SERVER=${DNS_SERVER}

# ==== Ports ====
WEBFS_PORT=${WEBFS_PORT}
IPXE_MANAGER_PORT=${IPXE_MANAGER_PORT}
CCP_PORT=${CCP_PORT}
MONITOR_PORT=${MONITOR_PORT}
MONITOR_REFRESH=${MONITOR_REFRESH:-30}

# ==== iPXE Manager ====
# Optional: set a password to protect the web UI and API (menu.ipxe stays open).
# Quoted so a password containing spaces survives sourcing and compose parsing.
IPXE_MANAGER_PASSWORD="${IPXE_MANAGER_PASSWORD:-}"

# ==== Cluster Control Panel (CCP) ====
CCP_ADMIN_USER=${CCP_ADMIN_USER}
CCP_ADMIN_PASSWORD="${CCP_ADMIN_PASSWORD}"
CCP_DEMO=${CCP_DEMO:-0}
CCP_SECRET_KEY="${CCP_SECRET_KEY}"

# ==== Monitor dashboard (login + RBAC) ====
# The dashboard admin reuses CCP_ADMIN_USER / CCP_ADMIN_PASSWORD above.
# Optionally set a read-only viewer account (leave password blank to disable).
MONITOR_SESSION_MINUTES=${MONITOR_SESSION_MINUTES:-30}
MONITOR_VIEWER_USER=${MONITOR_VIEWER_USER:-viewer}
MONITOR_VIEWER_PASSWORD="${MONITOR_VIEWER_PASSWORD:-}"
MONITOR_SECRET_KEY="${MONITOR_SECRET_KEY}"
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

  PXE_RANGE_START="$(prompt_ip "PXE_RANGE_START" "${PXE_RANGE_START:-192.168.100.10}")"
  PXE_RANGE_END="$(prompt_ip "PXE_RANGE_END" "${PXE_RANGE_END:-192.168.100.200}")"
  PXE_NETMASK="$(prompt_ip "PXE_NETMASK" "${PXE_NETMASK:-255.255.255.0}")"
  while true; do
    PXE_LEASE_TIME="$(prompt "PXE_LEASE_TIME (e.g. 45m, 12h, 1d, infinite)" "${PXE_LEASE_TIME:-12h}")"
    [[ "$PXE_LEASE_TIME" =~ ^([0-9]+[smhd]?|infinite)$ ]] && break
    warn "Invalid lease time. Use a number with an s/m/h/d suffix (e.g. 12h) or 'infinite'."
  done
  PXE_ROUTER_IP="$(prompt_ip "PXE_ROUTER_IP (gateway for PXE clients)" "${PXE_ROUTER_IP:-192.168.100.1}")"

  WEBFS_HOST_IP="$(prompt_ip "WEBFS_HOST_IP (clients reach webfs here)" "${WEBFS_HOST_IP:-192.168.100.1}")"
  TFTP_SERVER_IP="$(prompt_ip "TFTP_SERVER_IP" "${TFTP_SERVER_IP:-192.168.100.1}")"
  DNS_SERVER="$(prompt_ip "DNS_SERVER" "${DNS_SERVER:-8.8.8.8}")"

  WEBFS_PORT="$(prompt "WEBFS_PORT" "${WEBFS_PORT:-8080}")"
  IPXE_MANAGER_PORT="$(prompt "IPXE_MANAGER_PORT" "${IPXE_MANAGER_PORT:-8091}")"
  CCP_PORT="$(prompt "CCP_PORT (Cluster Control Panel)" "${CCP_PORT:-8060}")"
  MONITOR_PORT="$(prompt "MONITOR_PORT" "${MONITOR_PORT:-8090}")"

  # Cluster Control Panel admin credentials
  CCP_ADMIN_USER="$(prompt "CCP_ADMIN_USER (Control Panel admin login)" "${CCP_ADMIN_USER:-admin}")"
  while true; do
    CCP_ADMIN_PASSWORD="$(prompt "CCP_ADMIN_PASSWORD (min 8 chars)")"
    if [[ "${#CCP_ADMIN_PASSWORD}" -lt 8 ]]; then
      warn "Password must be at least 8 characters."; continue
    fi
    # these characters break the double-quoted value in .env / shell sourcing
    if [[ "$CCP_ADMIN_PASSWORD" == *['"\$`']* ]]; then
      warn "Please avoid the characters  \"  \\  \$  \`  in the password."; continue
    fi
    break
  done
  CCP_DEMO="${CCP_DEMO:-0}"

  # Flask session-signing keys — generated once, then reused across runs
  if [[ -z "${CCP_SECRET_KEY:-}" ]]; then
    CCP_SECRET_KEY="$(gen_secret)"
    log "Generated CCP_SECRET_KEY."
  fi
  if [[ -z "${MONITOR_SECRET_KEY:-}" ]]; then
    MONITOR_SECRET_KEY="$(gen_secret)"
    log "Generated MONITOR_SECRET_KEY."
  fi

  write_env
  load_env
}

fetch_ipxe_binaries() {
  local dst="services/tftp/tftpboot"
  mkdir -p "$dst"

  if prompt_yesno "Download iPXE binaries (undionly.kpxe, ipxe.efi) into $dst now?" "Y"; then
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
  docker_cli compose up -d --build
  log "Stack started. (restart: unless-stopped is set on all services)"
}

enable_ip_forwarding() {
  log "Enabling IPv4 forwarding permanently..."
  sudo_run bash -c 'echo "net.ipv4.ip_forward=1" > /etc/sysctl.d/99-lab-nat.conf'
  sudo_run sysctl --system >/dev/null
}

enable_nat() {
  enable_ip_forwarding
  sudo_run apt-get update -y >/dev/null 2>&1 || true
  sudo_run apt-get install -y iptables >/dev/null 2>&1 || true

  # interface names are baked into a tiny config the NAT script sources, so the
  # script itself is fully static (written from a quoted heredoc, no expansion)
  log "Writing /etc/lab-nat.conf"
  sudo_run bash -c "printf 'WAN_IFACE=%s\nPXE_IFACE=%s\n' '${WAN_IFACE}' '${PXE_IFACE}' > /etc/lab-nat.conf"

  local tmp; tmp="$(mktemp)"
  cat > "$tmp" <<'LABNAT'
#!/usr/bin/env bash
# Lab NAT: masquerade PXE/LAB clients out the WAN interface and permit
# forwarding. Docker forces the filter FORWARD policy to DROP, so an ACCEPT in
# a private table is not enough — the rules must live in DOCKER-USER, which
# Docker evaluates (and preserves) ahead of its own rules. If DOCKER-USER is
# absent (Docker not managing iptables) we fall back to the FORWARD chain.
# Idempotent; works with both the iptables-legacy and iptables-nft backends.
set -euo pipefail
[ -r /etc/lab-nat.conf ] && . /etc/lab-nat.conf
: "${WAN_IFACE:?WAN_IFACE not set}" ; : "${PXE_IFACE:?PXE_IFACE not set}"
ACTION="${1:-up}"

if iptables -L DOCKER-USER -n >/dev/null 2>&1; then FCHAIN=DOCKER-USER; else FCHAIN=FORWARD; fi

if [ "$ACTION" = "down" ]; then
  iptables -t nat -D POSTROUTING -o "$WAN_IFACE" -j MASQUERADE 2>/dev/null || true
  iptables -D "$FCHAIN" -i "$PXE_IFACE" -o "$WAN_IFACE" -j ACCEPT 2>/dev/null || true
  iptables -D "$FCHAIN" -i "$WAN_IFACE" -o "$PXE_IFACE" -m state --state RELATED,ESTABLISHED -j ACCEPT 2>/dev/null || true
  exit 0
fi

iptables -t nat -C POSTROUTING -o "$WAN_IFACE" -j MASQUERADE 2>/dev/null \
  || iptables -t nat -A POSTROUTING -o "$WAN_IFACE" -j MASQUERADE
iptables -C "$FCHAIN" -i "$PXE_IFACE" -o "$WAN_IFACE" -j ACCEPT 2>/dev/null \
  || iptables -I "$FCHAIN" -i "$PXE_IFACE" -o "$WAN_IFACE" -j ACCEPT
iptables -C "$FCHAIN" -i "$WAN_IFACE" -o "$PXE_IFACE" -m state --state RELATED,ESTABLISHED -j ACCEPT 2>/dev/null \
  || iptables -I "$FCHAIN" -i "$WAN_IFACE" -o "$PXE_IFACE" -m state --state RELATED,ESTABLISHED -j ACCEPT
LABNAT
  sudo_run install -m 0755 "$tmp" /usr/local/sbin/lab-nat.sh
  rm -f "$tmp"

  local unit="/etc/systemd/system/lab-nat.service"
  tmp="$(mktemp)"
  cat > "$tmp" <<'LABUNIT'
[Unit]
Description=Lab NAT for PXE/LAB clients
After=docker.service network-online.target
Wants=network-online.target
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/lab-nat.sh up
ExecReload=/usr/local/sbin/lab-nat.sh up
ExecStop=/usr/local/sbin/lab-nat.sh down

[Install]
WantedBy=multi-user.target
LABUNIT
  sudo_run install -m 0644 "$tmp" "$unit"
  rm -f "$tmp"

  sudo_run systemctl daemon-reload
  sudo_run systemctl enable --now lab-nat.service
  log "NAT enabled. Verify:  sudo iptables -S DOCKER-USER ; sudo iptables -t nat -S POSTROUTING"
}

nat_wizard() {
  if prompt_yesno "Enable persistent NAT for PXE/LAB clients (recommended)?" "Y"; then
    enable_nat
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
Description=Lab Stack (docker compose)
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
  docker_cli ps || true

  if have curl; then
    local url="http://127.0.0.1:${WEBFS_PORT:-8080}/files/"
    log "Checking Webfs /files/ : $url"
    curl -fsSI "$url" >/dev/null 2>&1 && log "Webfs OK" || warn "Webfs not reachable yet (may take a few seconds)."
  fi
}

main() {
  log "Lab deploy wizard starting..."
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
    # Ensure the session-signing keys exist even if the wizard was skipped.
    ensure_env_secret CCP_SECRET_KEY
    ensure_env_secret MONITOR_SECRET_KEY
    [[ -n "${CCP_ADMIN_PASSWORD:-}" ]] || \
      warn "CCP_ADMIN_PASSWORD is empty in .env — set it before the Control Panel / dashboard login is usable."
    load_env
    log "Using existing .env as-is."
  fi

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
  echo "Monitor:       http://${WEBFS_HOST_IP:-<host>}:${MONITOR_PORT:-8090}/   (service health + DHCP leases)"
  echo "Control Panel: http://${WEBFS_HOST_IP:-<host>}:${CCP_PORT:-8060}/   (ClusterShell + Ansible, login: ${CCP_ADMIN_USER:-admin})"
  echo
  warn "Reminder: DHCP is running on PXE_IFACE=${PXE_IFACE:-<PXE_IFACE>}. Ensure no other DHCP server exists on that lab segment."
}

main "$@"
