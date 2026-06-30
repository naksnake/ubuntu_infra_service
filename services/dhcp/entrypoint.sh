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
    echo "==== Starting dnsmasq (attempt $attempt) ===="
    dnsmasq --no-daemon || true

    echo "==== dnsmasq exited ===="
    if [ "$attempt" -ge "$MAX_RETRIES" ]; then
        echo "==== Max retries ($MAX_RETRIES) reached. Sleeping 60s before reset. ===="
        sleep 60
        attempt=0
    else
        echo "==== Retrying in ${RETRY_DELAY}s (attempt $attempt / $MAX_RETRIES) ===="
        sleep "$RETRY_DELAY"
    fi
done
