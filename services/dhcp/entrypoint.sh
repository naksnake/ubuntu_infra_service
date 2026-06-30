#!/bin/sh
set -eu

# Render config from environment
envsubst < /etc/dnsmasq.conf.template > /etc/dnsmasq.conf
echo "==== Rendered /etc/dnsmasq.conf ===="
cat /etc/dnsmasq.conf

MAX_RETRIES=5
RETRY_DELAY=5
attempt=0

while true; do
    attempt=$((attempt + 1))
    echo "==== Starting dnsmasq (attempt $attempt / $MAX_RETRIES) ===="
    dnsmasq --no-daemon
    rc=$?
    echo "==== dnsmasq exited (rc=$rc) ===="
    if [ "$attempt" -ge "$MAX_RETRIES" ]; then
        echo "==== Max retries reached — exiting so Docker can apply restart policy ===="
        exit 1
    fi
    echo "==== Retrying in ${RETRY_DELAY}s ===="
    sleep "$RETRY_DELAY"
done
