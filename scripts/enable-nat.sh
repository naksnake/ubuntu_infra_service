
#!/usr/bin/env bash
set -euo pipefail
[ -f .env ] || { echo ".env not found"; exit 1; }
set -a; source .env; set +a
: "${WAN_IFACE:?}"; : "${PXE_IFACE:?}"

# Temporary NAT (non-persistent)
sudo sysctl -w net.ipv4.ip_forward=1
sudo iptables -t nat -A POSTROUTING -o "$WAN_IFACE" -j MASQUERADE
sudo iptables -A FORWARD -i "$WAN_IFACE" -o "$PXE_IFACE" -m state --state RELATED,ESTABLISHED -j ACCEPT
sudo iptables -A FORWARD -i "$PXE_IFACE" -o "$WAN_IFACE" -j ACCEPT

echo "Temporary NAT enabled (iptables). Use persist-nat-nftables.sh or persist-nat-iptables.sh for persistence."
