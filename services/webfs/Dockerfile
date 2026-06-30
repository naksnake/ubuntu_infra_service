FROM debian:bookworm-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends webfs ca-certificates wget && \
    rm -rf /var/lib/apt/lists/*

RUN mkdir -p /var/www/htdocs && chown -R www-data:www-data /var/www/htdocs

COPY webfs.conf /etc/webfsd.conf
COPY htdocs/index.html /var/www/htdocs/index.html

EXPOSE 8080

CMD ["webfsd", "-F", "-b", "0.0.0.0", "-p", "8080", "-r", "/var/www/htdocs", "-c", "/etc/webfsd.conf"]
