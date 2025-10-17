
#!/bin/sh
set -eu
envsubst < /etc/dnsmasq.conf.template > /etc/dnsmasq.conf
echo "==== Rendered /etc/dnsmasq.conf ===="
cat /etc/dnsmasq.conf
exec dnsmasq --no-daemon
