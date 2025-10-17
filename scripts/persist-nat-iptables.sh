
#!/usr/bin/env bash
set -euo pipefail
[ -f .env ] || { echo ".env not found"; exit 1; }
set -a; source .env; set +a
: "${WAN_IFACE:?}"; : "${PXE_IFACE:?}"

SYSCTL_FILE=/etc/sysctl.d/99-sit-nat.conf
RULES_V4=/etc/iptables/rules.v4

sudo mkdir -p /etc/iptables

cat <<EOF | sudo tee "$SYSCTL_FILE" >/dev/null
net.ipv4.ip_forward=1
EOF
sudo sysctl --system >/dev/null

cat <<EOF | sudo tee "$RULES_V4" >/dev/null
*filter
:INPUT ACCEPT [0:0]
:FORWARD DROP [0:0]
:OUTPUT ACCEPT [0:0]
-A FORWARD -i ${PXE_IFACE} -o ${WAN_IFACE} -j ACCEPT
-A FORWARD -i ${WAN_IFACE} -o ${PXE_IFACE} -m state --state RELATED,ESTABLISHED -j ACCEPT
COMMIT
*nat
:PREROUTING ACCEPT [0:0]
:INPUT ACCEPT [0:0]
:OUTPUT ACCEPT [0:0]
:POSTROUTING ACCEPT [0:0]
-A POSTROUTING -o ${WAN_IFACE} -j MASQUERADE
COMMIT
EOF

if command -v iptables-restore >/dev/null 2>&1; then
  sudo iptables-restore < "$RULES_V4"
else
  echo "WARNING: iptables-restore not found. Please install iptables-persistent or netfilter-persistent."
fi

echo "Persistent NAT rules written to $RULES_V4. Install/enable iptables-persistent to load at boot."
