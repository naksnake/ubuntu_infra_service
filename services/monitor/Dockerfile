FROM python:3.12-slim

RUN pip install --no-cache-dir flask==3.0.3 docker==7.1.0

WORKDIR /app
COPY app.py .
COPY templates/ templates/

EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/api/status')" || exit 1

CMD ["python", "app.py"]
