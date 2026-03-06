#!/bin/bash
set -e

# Sync DB password on every startup (initdb only sets it on first run)
(
    until pg_isready -U "${POSTGRES_USER:-sentinel}" -q 2>/dev/null; do sleep 1; done
    psql -U "$POSTGRES_USER" -d "${POSTGRES_DB:-$POSTGRES_USER}" \
        -c "ALTER USER \"$POSTGRES_USER\" WITH PASSWORD '$POSTGRES_PASSWORD';" 2>/dev/null || true
) &

exec docker-entrypoint.sh postgres "$@"
