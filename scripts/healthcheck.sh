#!/bin/bash
# healthcheck.sh — Container health check script
# Usage: healthcheck.sh <service_name>
set -euo pipefail

SERVICE="${1:-unknown}"

case "$SERVICE" in
    collectors)
        [ -f /tmp/collectors_healthy ] && exit 0 || exit 1
        ;;
    ml)
        [ -f /tmp/ml_healthy ] && exit 0 || exit 1
        ;;
    api)
        curl -sf http://localhost:8000/health > /dev/null 2>&1 && exit 0 || exit 1
        ;;
    *)
        echo "Unknown service: $SERVICE"
        exit 1
        ;;
esac
