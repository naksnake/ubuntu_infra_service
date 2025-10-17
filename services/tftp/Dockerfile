
FROM debian:bookworm-slim
RUN apt-get update     && apt-get install -y --no-install-recommends tftpd-hpa     && rm -rf /var/lib/apt/lists/*
RUN mkdir -p /srv/tftp
COPY tftpd-hpa.conf /etc/default/tftpd-hpa
EXPOSE 69/udp
CMD ["/usr/sbin/in.tftpd","--foreground","--secure","/srv/tftp"]
