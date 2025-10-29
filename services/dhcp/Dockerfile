FROM debian:bookworm-slim

# Install dnsmasq and gettext-base, clean up APT cache
RUN apt-get update && \
    apt-get install -y --no-install-recommends dnsmasq gettext-base && \
    rm -rf /var/lib/apt/lists/*

# Copy configuration and entrypoint script
COPY dnsmasq.conf.template /etc/dnsmasq.conf.template
COPY entrypoint.sh /entrypoint.sh

# Make entrypoint executable and create TFTP directory
RUN chmod +x /entrypoint.sh && mkdir -p /srv/tftp

# Expose DHCP and PXE ports
EXPOSE 67/udp 4011/udp

# Start the container
CMD ["/entrypoint.sh"]
