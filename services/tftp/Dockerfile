FROM debian:bookworm-slim

# Install tftpd-hpa
RUN apt-get update && \
    apt-get install -y --no-install-recommends tftpd-hpa && \
    rm -rf /var/lib/apt/lists/*

# Create TFTP root directory
RUN mkdir -p /srv/tftp

# Copy configuration file
COPY tftpd-hpa.conf /etc/default/tftpd-hpa

# Expose TFTP port
EXPOSE 69/udp

# Run TFTP server in foreground
CMD ["/usr/sbin/in.tftpd", "--foreground", "--secure", "/srv/tftp"]
