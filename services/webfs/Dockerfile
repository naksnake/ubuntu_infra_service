
FROM debian:bookworm-slim
RUN apt-get update     && apt-get install -y --no-install-recommends webfs ca-certificates     && rm -rf /var/lib/apt/lists/*
COPY webfs.conf /etc/webfsd.conf
RUN mkdir -p /var/www/htdocs && chown -R www-data:www-data /var/www/htdocs
EXPOSE 8080
CMD ["webfsd","-F","-b","0.0.0.0","-p","8080","-r","/var/www/htdocs","-c","/etc/webfsd.conf"]
