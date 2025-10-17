
FROM debian:bookworm-slim
RUN apt-get update     && apt-get install -y --no-install-recommends dnsmasq gettext-base     && rm -rf /var/lib/apt/lists/*
COPY dnsmasq.conf.template /etc/dnsmasq.conf.template
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && mkdir -p /srv/tftp
EXPOSE 67/udp 4011/udp
CMD ["/entrypoint.sh"]
