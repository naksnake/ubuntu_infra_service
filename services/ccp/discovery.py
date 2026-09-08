"""DHCP lease discovery: parse the dnsmasq lease database (written by the
lab_dhcp container, bind-mounted read-only into CCP) and cross-reference it
against the nodes table, so machines that PXE-booted onto the lab network can
be imported into the inventory without manual entry.

Lease line formats (same file the monitor dashboard parses):
  DHCPv4: <expiry> <mac> <ip> <hostname|*> <client-id>
  DHCPv6: <expiry> <iaid> <ipv6> <hostname|*> <duid>
An expiry of 0 means an infinite lease (static reservation).
"""
import os
import time

import db

LEASES_FILE = os.environ.get('CCP_LEASES_FILE', '/data/dnsmasq.leases')


def _ip_sort_key(lease):
    octets = lease['ip'].split('.')
    if len(octets) == 4 and all(o.isdigit() for o in octets):
        return [int(o) for o in octets]
    return [999]


def parse_leases(path=None):
    """Parse the dnsmasq lease file. Returns (leases, error) where each lease
    is {ip, mac, hostname ('' when the client sent none), expiry_ts, expired}.
    Malformed lines are skipped, never fatal."""
    path = path or LEASES_FILE
    if not os.path.exists(path):
        return [], (f'leases file not found: {path} — is the dhcp service '
                    'running and the bind mount present?')
    leases, now = [], int(time.time())
    try:
        with open(path) as fh:
            for line in fh:
                parts = line.strip().split()
                if len(parts) < 4:
                    continue
                try:
                    expiry = int(parts[0])
                except ValueError:
                    continue
                mac, ip = parts[1].upper(), parts[2]
                hostname = parts[3] if parts[3] != '*' else ''
                if ':' in ip:
                    # DHCPv6 line: field 2 is an IAID, the DUID sits at the end
                    mac = parts[4].upper() if len(parts) > 4 else ''
                leases.append({
                    'ip': ip, 'mac': mac, 'hostname': hostname,
                    'expiry_ts': expiry,
                    'expired': expiry != 0 and expiry <= now,  # 0 = infinite
                })
    except OSError as exc:
        return [], f'could not read leases: {exc}'
    leases.sort(key=_ip_sort_key)
    return leases, None


def annotate(leases):
    """Attach inventory status to each lease: node_id/node_name/node_state when
    a node already matches by MAC (preferred — IPs churn) or by address."""
    nodes = db.query('SELECT id, name, address, mac, state FROM nodes')
    by_mac = {n['mac'].upper(): n for n in nodes if n['mac']}
    by_addr = {n['address']: n for n in nodes}
    for lease in leases:
        n = by_mac.get(lease['mac']) or by_addr.get(lease['ip'])
        lease['node_id'] = n['id'] if n else None
        lease['node_name'] = n['name'] if n else ''
        lease['node_state'] = n['state'] if n else ''
    return leases


def new_system_count():
    """How many active leases have no matching inventory node (dashboard)."""
    leases, err = parse_leases()
    if err:
        return 0
    return sum(1 for l in annotate(leases) if not l['node_id'] and not l['expired'])
