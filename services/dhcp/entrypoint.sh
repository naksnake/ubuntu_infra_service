#!/bin/sh
set -eu

# Default the lease time so a .env that predates PXE_LEASE_TIME still renders
# a valid dhcp-range line (envsubst would otherwise leave the field empty).
export PXE_LEASE_TIME="${PXE_LEASE_TIME:-12h}"

# Render config from environment
envsubst < /etc/dnsmasq.conf.template > /etc/dnsmasq.conf

# IPv6 is opt-in: PXE_ENABLE_IPV6=1 appends stateful DHCPv6 + router
# advertisements for the lab segment; anything else leaves the rendered
# config IPv4-only, so dnsmasq opens no DHCPv6/RA sockets at all.
if [ "${PXE_ENABLE_IPV6:-0}" = "1" ]; then
    if [ -z "${PXE_IPV6_RANGE_START:-}" ] || [ -z "${PXE_IPV6_RANGE_END:-}" ]; then
        echo "ERROR: PXE_ENABLE_IPV6=1 requires PXE_IPV6_RANGE_START and PXE_IPV6_RANGE_END" >&2
        exit 1
    fi
    {
        echo ""
        echo "# ---- IPv6 (appended by entrypoint: PXE_ENABLE_IPV6=1) ----"
        echo "# Stateful DHCPv6 pool; enable-ra announces the route so clients use it."
        echo "# Requires an interface address inside this prefix (PXE_ROUTER_IP6 —"
        echo "# assigned by the deploy.sh IP watchdog), or dnsmasq ignores the range."
        echo "dhcp-range=${PXE_IPV6_RANGE_START},${PXE_IPV6_RANGE_END},${PXE_IPV6_PREFIX_LEN:-64},${PXE_LEASE_TIME}"
        echo "enable-ra"
    } >> /etc/dnsmasq.conf
fi

# Compose bind-mounts the reservations file; when the image runs standalone,
# make sure it exists so dnsmasq does not abort on a missing dhcp-hostsfile.
[ -f /etc/dnsmasq-static-hosts.conf ] || touch /etc/dnsmasq-static-hosts.conf
echo "==== Rendered /etc/dnsmasq.conf ===="
cat /etc/dnsmasq.conf

MAX_RETRIES=5
RETRY_DELAY=5
attempt=0

while true; do
    attempt=$((attempt + 1))
    echo "==== Starting dnsmasq (attempt $attempt / $MAX_RETRIES) ===="
    dnsmasq --no-daemon || rc=$?
    echo "==== dnsmasq exited (rc=${rc:-0}) ===="
    if [ "$attempt" -ge "$MAX_RETRIES" ]; then
        echo "==== Max retries reached — exiting so Docker can apply restart policy ===="
        exit 1
    fi
    echo "==== Retrying in ${RETRY_DELAY}s ===="
    sleep "$RETRY_DELAY"
done
