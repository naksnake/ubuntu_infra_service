FROM python:3.12-slim

# requests: server-side proxy of dashboard file uploads to the iPXE Manager
RUN pip install --no-cache-dir flask==3.0.3 docker==7.1.0 gunicorn==22.0.0 requests==2.32.3

WORKDIR /app
COPY app.py .
COPY templates/ templates/

EXPOSE 8090

# /healthz is the only endpoint that stays open without a login
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/healthz')" || exit 1

# gthread keeps the arbiter heartbeat off the request thread, so a multi-GB
# upload proxied to the iPXE Manager is not killed at --timeout (sync is)
CMD ["gunicorn", "-w", "2", "-k", "gthread", "--threads", "8", \
     "--timeout", "120", "-b", "0.0.0.0:8090", "app:app"]
