FROM debian:bookworm-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends webfs ca-certificates wget && \
    rm -rf /var/lib/apt/lists/*

RUN mkdir -p /var/www/htdocs && chown -R www-data:www-data /var/www/htdocs

COPY htdocs/index.html /var/www/htdocs/index.html

EXPOSE 8080

# webfsd flags: -b is basic-auth (user:pass) and -c is max-connections —
# neither is a bind address nor a config file. Binds all interfaces by default.
CMD ["webfsd", "-F", "-p", "8080", "-r", "/var/www/htdocs", "-f", "index.html"]
