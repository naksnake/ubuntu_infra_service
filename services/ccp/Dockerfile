FROM python:3.12-slim

# openssh-client: ClusterShell + ansible reach nodes over SSH
# sshpass:        optional password auth for ansible/ssh
RUN apt-get update && \
    apt-get install -y --no-install-recommends openssh-client sshpass ca-certificates && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
# Flaky or slow link to PyPI: retries + a long timeout ride out stalls.
# Set PIP_MIRROR in .env (e.g. a regional or in-house mirror) to install from
# there; PIP_MIRROR_HOST additionally trusts a plain-http mirror host.
ARG PIP_MIRROR=
ARG PIP_MIRROR_HOST=
RUN pip install --no-cache-dir --retries 10 --timeout 120 \
      ${PIP_MIRROR:+--index-url "$PIP_MIRROR"} \
      ${PIP_MIRROR_HOST:+--trusted-host "$PIP_MIRROR_HOST"} \
      -r requirements.txt

COPY db.py executor.py app.py ansible_sources.py discovery.py topology.py rack_layout.py ./
COPY templates/ templates/
COPY static/ static/

EXPOSE 8060

HEALTHCHECK --interval=30s --timeout=5s --start-period=8s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8060/healthz')" || exit 1

# single worker so background job threads share process state with the API;
# gthread keeps long-running jobs from blocking request handling.
CMD ["gunicorn", "-w", "1", "-k", "gthread", "--threads", "8", \
     "--timeout", "300", "-b", "0.0.0.0:8060", "app:app"]
