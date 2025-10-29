FROM debian:bookworm-slim

# Install webfs and certificates
RUN apt-get update &&     apt-get install -y --no-install-recommends webfs ca-certificates &&     rm -rf /var/lib/apt/lists/*

# Create web root and set permissions
RUN mkdir -p /var/www/htdocs &&     chown -R www-data:www-data /var/www/htdocs

# Copy configuration and index.html
COPY webfsd.conf /etc/webfsd.conf
COPY index.html /var/www/htdocs/index.html

# Expose HTTP port
EXPOSE 8080

# Run webfsd in foreground
CMD ["webfsd", "-F", "-b", "0.0.0.0", "-p", "8080", "-r", "/var/www/htdocs", "-c", "/etc/webfsd.conf"]
