#!/usr/bin/env bash
# Update PXE_RANGE_START / PXE_RANGE_END in .env and restart only the dhcp container.
# Usage:
#   ./update-dhcp-range.sh <start-ip> <end-ip>
#   ./update-dhcp-range.sh 192.168.100.50 192.168.100.150
#   ./update-dhcp-range.sh           (interactive)
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

[[ -f .env ]] || die ".env not found. Run deploy.sh first."

NEW_START="${1:-}"
NEW_END="${2:-}"

if [[ -z "$NEW_START" || -z "$NEW_END" ]]; then
  # shellcheck disable=SC1091
  set -a; . ./.env; set +a
  log "Current range: ${PXE_RANGE_START} – ${PXE_RANGE_END}"
  read -r -p "New PXE_RANGE_START [${PXE_RANGE_START}]: " ans
  NEW_START="${ans:-$PXE_RANGE_START}"
  read -r -p "New PXE_RANGE_END   [${PXE_RANGE_END}]: " ans
  NEW_END="${ans:-$PXE_RANGE_END}"
fi

validate_ip "$NEW_START"
validate_ip "$NEW_END"

log "Updating .env: $NEW_START – $NEW_END"
_tmp="$(mktemp "${ROOT_DIR}/.env.XXXXXX")"
sed \
  -e "s|^PXE_RANGE_START=.*|PXE_RANGE_START=${NEW_START}|" \
  -e "s|^PXE_RANGE_END=.*|PXE_RANGE_END=${NEW_END}|" \
  .env > "$_tmp"
mv "$_tmp" .env

log "Restarting DHCP container..."
docker compose up -d --no-deps --force-recreate dhcp

log "Done. New DHCP range: ${NEW_START} – ${NEW_END}"
log "Verify: docker logs sit_dhcp | head -40"
