FROM python:3.12-slim

RUN pip install --no-cache-dir flask==3.0.3 gunicorn==22.0.0

WORKDIR /app
COPY app.py .
COPY templates/ templates/

EXPOSE 8091

# /menu.ipxe stays reachable even when AUTH_PASSWORD protects the API
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8091/menu.ipxe')" || exit 1

CMD ["python", "app.py"]
