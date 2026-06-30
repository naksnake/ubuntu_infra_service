#!/usr/bin/env bash
# AWX superuser post-init helper.
# Called by deploy.sh after AWX containers are healthy.
# Safe to run multiple times — creates user if missing, updates password.
set -euo pipefail

AWX_ADMIN_USER="${AWX_ADMIN_USER:-admin}"
AWX_ADMIN_PASSWORD="${AWX_ADMIN_PASSWORD:-}"
AWX_ADMIN_EMAIL="${AWX_ADMIN_EMAIL:-admin@lab.local}"

[[ -n "$AWX_ADMIN_PASSWORD" ]] || { echo "AWX_ADMIN_PASSWORD not set" >&2; exit 1; }

docker exec lab_awx_task \
  awx-manage createsuperuser \
    --noinput \
    --username "$AWX_ADMIN_USER" \
    --email "$AWX_ADMIN_EMAIL" 2>/dev/null || true

docker exec lab_awx_task \
  awx-manage update_password \
    --username "$AWX_ADMIN_USER" \
    --password "$AWX_ADMIN_PASSWORD"

echo "AWX superuser '${AWX_ADMIN_USER}' ready."
