#!/usr/bin/env bash
# Update the DHCP pool(s) in .env and restart only the dhcp container.
# The address family is auto-detected: IPv4 arguments update
# PXE_RANGE_START/PXE_RANGE_END, IPv6 arguments update
# PXE_IPV6_RANGE_START/PXE_IPV6_RANGE_END (served when PXE_ENABLE_IPV6=1;
# see .env.example).
# Usage:
#   ./update-dhcp-range.sh <start-ip> <end-ip>
#   ./update-dhcp-range.sh 192.168.100.50 192.168.100.150    # IPv4 pool
#   ./update-dhcp-range.sh fd00:100::10 fd00:100::4ff        # IPv6 pool
#   ./update-dhcp-range.sh    (interactive: IPv4, plus IPv6 when enabled)
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

GREEN="\033[0;32m"; YELLOW="\033[0;33m"; RED="\033[0;31m"; NC="\033[0m"
log()  { echo -e "${GREEN}[dhcp-range]${NC} $*"; }
warn() { echo -e "${YELLOW}[dhcp-range]${NC} $*" >&2; }
die()  { echo -e "${RED}[dhcp-range] ERROR:${NC} $*" >&2; exit 1; }

validate_ip() {
  local ip="$1"
  local re='^([0-9]{1,3}\.){3}[0-9]{1,3}$'
  [[ "$ip" =~ $re ]] || die "Invalid IP address: $ip"
  IFS='.' read -r o1 o2 o3 o4 <<< "$ip"
  for oct in "$o1" "$o2" "$o3" "$o4"; do
    (( oct >= 0 && oct <= 255 )) || die "Octet out of range in: $ip"
  done
}

# Pragmatic IPv6 validation (same rules as deploy.sh): hex groups, right
# group count, at most one '::' — dnsmasq re-validates at startup.
valid_ipv6() {
  local ip="$1" g n=0
  [[ "$ip" =~ ^[0-9A-Fa-f:]+$ && "$ip" == *:* && "$ip" != *:::* ]] || return 1
  [[ "$(grep -o '::' <<< "$ip" | wc -l)" -le 1 ]] || return 1
  # a lone leading/trailing colon is only valid as part of '::'
  [[ "$ip" != :* || "$ip" == ::* ]] || return 1
  [[ "$ip" != *: || "$ip" == *:: ]] || return 1
  local IFS=':'
  for g in $ip; do
    [[ "${#g}" -le 4 ]] || return 1
    n=$((n + 1))
  done
  if [[ "$ip" == *::* ]]; then (( n <= 8 )); else (( n == 8 )); fi
}

ip_to_int() {
  local o1 o2 o3 o4
  IFS='.' read -r o1 o2 o3 o4 <<< "$1"
  echo $(( (o1 << 24) | (o2 << 16) | (o3 << 8) | o4 ))
}

# Canonical IPv6 sort key: expand '::', zero-pad every group to 4 hex digits
# and concatenate — the equal-length lowercase strings then compare correctly
# as plain strings, which is how the start<=end check works.
ipv6_key() {
  local ip="$1" head="" tail="" g out=""
  if [[ "$ip" == *::* ]]; then
    head="${ip%%::*}"; tail="${ip#*::}"
  else
    head="$ip"
  fi
  local -a hg=() tg=() groups=()
  [[ -n "$head" ]] && IFS=':' read -ra hg <<< "$head"
  [[ -n "$tail" ]] && IFS=':' read -ra tg <<< "$tail"
  local fill=$(( 8 - ${#hg[@]} - ${#tg[@]} )) i
  groups=("${hg[@]}")
  for (( i = 0; i < fill; i++ )); do groups+=(0); done
  groups+=("${tg[@]}")
  for g in "${groups[@]}"; do out+="$(printf '%04x' "$(( 16#$g ))")"; done
  echo "$out"
}

# Replace KEY=... in .env, or append it when missing — a .env written before
# the IPv6 settings existed has no PXE_IPV6_* keys yet.
set_env_var() {
  local key="$1" val="$2" tmp
  tmp="$(mktemp "${ROOT_DIR}/.env.XXXXXX")"
  if grep -q "^${key}=" .env; then
    sed "s|^${key}=.*|${key}=${val}|" .env > "$tmp"
  else
    cp .env "$tmp"
    # guarantee a trailing newline before appending
    [[ -s "$tmp" && -z "$(tail -c1 "$tmp")" ]] || echo >> "$tmp"
    echo "${key}=${val}" >> "$tmp"
  fi
  mv "$tmp" .env
}

[[ -f .env ]] || die ".env not found. Run deploy.sh first."
set -a
# shellcheck disable=SC1091
. ./.env
set +a

NEW4_START=""; NEW4_END=""; NEW6_START=""; NEW6_END=""

if [[ -n "${1:-}" || -n "${2:-}" ]]; then
  [[ -n "${1:-}" && -n "${2:-}" ]] || die "Need both <start-ip> and <end-ip> (or no arguments for interactive mode)."
  if [[ "$1" == *:* || "$2" == *:* ]]; then
    [[ "$1" == *:* && "$2" == *:* ]] || die "Mixed address families: both addresses must be IPv4, or both IPv6."
    NEW6_START="$1"; NEW6_END="$2"
  else
    NEW4_START="$1"; NEW4_END="$2"
  fi
else
  log "Current IPv4 pool: ${PXE_RANGE_START} – ${PXE_RANGE_END}"
  read -r -p "New PXE_RANGE_START [${PXE_RANGE_START}]: " ans
  NEW4_START="${ans:-$PXE_RANGE_START}"
  read -r -p "New PXE_RANGE_END   [${PXE_RANGE_END}]: " ans
  NEW4_END="${ans:-$PXE_RANGE_END}"
  if [[ "${PXE_ENABLE_IPV6:-0}" == "1" ]]; then
    log "Current IPv6 pool: ${PXE_IPV6_RANGE_START:-<unset>} – ${PXE_IPV6_RANGE_END:-<unset>}"
    read -r -p "New PXE_IPV6_RANGE_START [${PXE_IPV6_RANGE_START:-}]: " ans
    NEW6_START="${ans:-${PXE_IPV6_RANGE_START:-}}"
    read -r -p "New PXE_IPV6_RANGE_END   [${PXE_IPV6_RANGE_END:-}]: " ans
    NEW6_END="${ans:-${PXE_IPV6_RANGE_END:-}}"
    if [[ -z "$NEW6_START" || -z "$NEW6_END" ]]; then
      warn "IPv6 pool left unchanged (no value given)."
      NEW6_START=""; NEW6_END=""
    fi
  fi
fi

if [[ -n "$NEW4_START" ]]; then
  validate_ip "$NEW4_START"
  validate_ip "$NEW4_END"
  (( $(ip_to_int "$NEW4_START") <= $(ip_to_int "$NEW4_END") )) \
    || die "Range start ($NEW4_START) must not be above range end ($NEW4_END)."
  log "Updating .env IPv4 pool: ${NEW4_START} – ${NEW4_END}"
  set_env_var PXE_RANGE_START "$NEW4_START"
  set_env_var PXE_RANGE_END   "$NEW4_END"
fi

if [[ -n "$NEW6_START" ]]; then
  valid_ipv6 "$NEW6_START" || die "Invalid IPv6 address: $NEW6_START"
  valid_ipv6 "$NEW6_END"   || die "Invalid IPv6 address: $NEW6_END"
  [[ "$(ipv6_key "$NEW6_START")" > "$(ipv6_key "$NEW6_END")" ]] \
    && die "Range start ($NEW6_START) must not be above range end ($NEW6_END)."
  log "Updating .env IPv6 pool: ${NEW6_START} – ${NEW6_END}"
  set_env_var PXE_IPV6_RANGE_START "$NEW6_START"
  set_env_var PXE_IPV6_RANGE_END   "$NEW6_END"
  if [[ "${PXE_ENABLE_IPV6:-0}" != "1" ]]; then
    warn "PXE_ENABLE_IPV6 is not 1 — the IPv6 pool is saved but NOT served yet."
    warn "Enable it with PXE_ENABLE_IPV6=1 in .env (see .env.example), then re-run ./deploy.sh"
    warn "(or: docker compose up -d --force-recreate dhcp && sudo systemctl start lab-ip-guard.service)."
  fi
fi

log "Restarting DHCP container..."
docker compose up -d --no-deps --force-recreate dhcp

if [[ -n "$NEW4_START" ]]; then log "Done. New IPv4 pool: ${NEW4_START} – ${NEW4_END}"; fi
if [[ -n "$NEW6_START" ]]; then log "Done. New IPv6 pool: ${NEW6_START} – ${NEW6_END}"; fi
log "Existing leases keep their addresses until they expire."
log "Verify: docker logs lab_dhcp | head -40"
