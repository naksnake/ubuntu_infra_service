FROM debian:bookworm-slim

# media-types provides /etc/mime.types — webfsd's default MIME table. It is
# only a Recommends of webfs, so --no-install-recommends drops it and every
# download would be served without a proper Content-Type.
RUN apt-get update && \
    apt-get install -y --no-install-recommends webfs media-types ca-certificates wget && \
    rm -rf /var/lib/apt/lists/*

# Pre-create the nested bind-mount targets (/ipxe, /files). The compose file
# mounts htdocs read-only, and a nested mount cannot mkdir its mountpoint on
# an ro filesystem — the directories must already exist on both sides.
RUN mkdir -p /var/www/htdocs/ipxe /var/www/htdocs/files /var/www/htdocs/linux && \
    chown -R www-data:www-data /var/www/htdocs

COPY htdocs/index.html /var/www/htdocs/index.html

EXPOSE 8080

# webfsd flags: -b is basic-auth (user:pass) and -c is max-connections —
# neither is a bind address nor a config file. Binds all interfaces by default.
# -m: default Content-Type for extensions missing from /etc/mime.types
# (.run installers, .ipxe, ...). webfsd's built-in default is text/plain,
# which makes browsers RENDER those files instead of downloading them.
CMD ["webfsd", "-F", "-p", "8080", "-r", "/var/www/htdocs", "-f", "index.html", \
     "-m", "application/octet-stream"]
