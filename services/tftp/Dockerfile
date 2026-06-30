FROM debian:bookworm-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends tftpd-hpa procps && \
    rm -rf /var/lib/apt/lists/*

RUN mkdir -p /srv/tftp

COPY tftpd-hpa.conf /etc/default/tftpd-hpa

EXPOSE 69/udp

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD pgrep in.tftpd >/dev/null || exit 1

CMD ["/usr/sbin/in.tftpd", "--foreground", "--secure", "/srv/tftp"]
