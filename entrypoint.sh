#!/bin/sh
# Role-based entrypoint. One image, two services, selected by $SERVICE_ROLE:
#   web  -> the always-on Setup Console (uvicorn)
#   sync -> the connector ELT job (run once, then exit; schedule via Railway cron)
# Unset -> idle no-op (safe default so an unconfigured service can't run the wrong thing).
set -e

case "${SERVICE_ROLE:-}" in
  web)
    exec uvicorn web.app:app --host 0.0.0.0 --port "${PORT:-8000}"
    ;;
  sync)
    python scripts/sync.py --init-db
    # SYNC_DOMAINS scopes the pull per connector (e.g. "mw_products" or "all").
    exec python scripts/sync.py --domain "${SYNC_DOMAINS:-all}" --mode "${SYNC_MODE:-incremental}" ${SYNC_EMAIL:+--email}
    ;;
  *)
    echo "SERVICE_ROLE not set (expected 'web' or 'sync') — container idle, exiting 0."
    exit 0
    ;;
esac
