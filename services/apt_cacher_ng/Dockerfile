FROM ubuntu:24.04

RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        apt-cacher-ng ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

RUN sed -ri 's|^# ?BindAddress:.*|BindAddress: 0.0.0.0|' /etc/apt-cacher-ng/acng.conf \
 && printf '\nPassThroughPattern: .*\n' >> /etc/apt-cacher-ng/acng.conf \
 && chown -R apt-cacher-ng:apt-cacher-ng /var/cache/apt-cacher-ng
RUN echo "ReportPage: acng-report/index.html" >> /etc/apt-cacher-ng/acng.conf

EXPOSE 3142
VOLUME ["/var/cache/apt-cacher-ng"]

CMD ["/usr/sbin/apt-cacher-ng", "-c", "/etc/apt-cacher-ng", "foreground=1"]

