FROM debian:bookworm-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends dnsmasq gettext-base procps && \
    rm -rf /var/lib/apt/lists/*

COPY dnsmasq.conf.template /etc/dnsmasq.conf.template
COPY entrypoint.sh /entrypoint.sh

RUN chmod +x /entrypoint.sh && mkdir -p /srv/tftp

EXPOSE 67/udp 4011/udp

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD pgrep dnsmasq >/dev/null || exit 1

CMD ["/entrypoint.sh"]
