#!/bin/sh
set -eu

# Default the lease time so a .env that predates PXE_LEASE_TIME still renders
# a valid dhcp-range line (envsubst would otherwise leave the field empty).
export PXE_LEASE_TIME="${PXE_LEASE_TIME:-12h}"

# Render config from environment
envsubst < /etc/dnsmasq.conf.template > /etc/dnsmasq.conf

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
