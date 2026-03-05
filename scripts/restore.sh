#!/bin/bash
# restore.sh — Restore Docker Sentinel from a backup archive
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

if [ $# -lt 1 ]; then
    echo "Usage: $0 <backup_file.tar.gz>"
    echo "Example: $0 backups/sentinel_backup_20240115_143301.tar.gz"
    exit 1
fi

BACKUP_FILE="$1"

if [ ! -f "$BACKUP_FILE" ]; then
    echo "Error: Backup file not found: $BACKUP_FILE"
    exit 1
fi

echo "=== Docker Sentinel Restore ==="
echo "Backup file: $BACKUP_FILE"
echo ""
read -p "This will overwrite all current data. Continue? (y/N) " -n 1 -r
echo ""

if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Restore cancelled."
    exit 0
fi

# ---------- Checksum validation ----------
CHECKSUM_FILE="${BACKUP_FILE}.sha256"
if [ -f "$CHECKSUM_FILE" ]; then
    echo "Validating checksum..."
    if sha256sum --check --status "$CHECKSUM_FILE" 2>/dev/null; then
        echo "  Checksum: OK"
    else
        echo "ERROR: Checksum verification FAILED — backup may be corrupt."
        exit 1
    fi
else
    echo "  Warning: No .sha256 checksum file found, skipping verification."
fi

# Verify archive integrity before proceeding
echo "Verifying archive integrity..."
if ! tar -tzf "$BACKUP_FILE" > /dev/null 2>&1; then
    echo "ERROR: Archive integrity check FAILED — file is corrupt."
    exit 1
fi
echo "  Archive integrity: OK"

# ---------- Pre-restore snapshot (rollback safety net) ----------
ROLLBACK_DIR="$PROJECT_DIR/backups"
ROLLBACK_FILE="$ROLLBACK_DIR/pre_restore_rollback_$(date +%Y%m%d_%H%M%S).tar.gz"
mkdir -p "$ROLLBACK_DIR"

echo "Creating pre-restore snapshot for rollback..."
cd "$PROJECT_DIR"

# Dump current database while running
ROLLBACK_DB_DUMP="$ROLLBACK_DIR/rollback_db_dump.sql"
if docker compose exec -T db \
    pg_dump -U "${DB_USER:-sentinel}" "${DB_NAME:-sentinel}" > "$ROLLBACK_DB_DUMP" 2>/dev/null; then
    echo "  Current database dumped"
else
    echo "  Warning: Could not dump current database (container may be down)"
    touch "$ROLLBACK_DB_DUMP"
fi

# Snapshot current data dirs
tar -czf "$ROLLBACK_FILE" \
    -C "$PROJECT_DIR" \
    data/redis/ data/models/ data/audit/ data/collector-state/ data/db/ config/ \
    2>/dev/null || true
if [ -s "$ROLLBACK_DB_DUMP" ]; then
    TEMP_ROLLBACK="${ROLLBACK_FILE%.gz}"
    gunzip -k "$ROLLBACK_FILE" 2>/dev/null && \
    tar -rf "$TEMP_ROLLBACK" -C "$ROLLBACK_DIR" "rollback_db_dump.sql" 2>/dev/null && \
    gzip -f "$TEMP_ROLLBACK" 2>/dev/null || true
fi
rm -f "$ROLLBACK_DB_DUMP"
echo "  Rollback snapshot: $ROLLBACK_FILE"

# ---------- Stop stack ----------
echo "Stopping stack..."
docker compose down 2>/dev/null || true

# ---------- Restore data ----------
echo "Restoring data..."
if ! tar -xzf "$BACKUP_FILE" -C "$PROJECT_DIR" --strip-components=1; then
    echo "ERROR: Extraction failed. Rolling back..."
    tar -xzf "$ROLLBACK_FILE" -C "$PROJECT_DIR" 2>/dev/null || true
    docker compose up -d
    echo "Rollback complete. Stack restored to previous state."
    exit 1
fi

# Fix permissions
chmod 700 data/redis/ 2>/dev/null || true

# ---------- Re-import PostgreSQL dump ----------
echo "Restarting stack..."
docker compose up -d

# Wait for database to accept connections
echo "Waiting for database..."
for i in $(seq 1 30); do
    if docker compose exec -T db pg_isready -U "${DB_USER:-sentinel}" > /dev/null 2>&1; then
        break
    fi
    sleep 1
done

# Check if backup contained a SQL dump and import it
SQL_DUMPS=$(tar -tzf "$BACKUP_FILE" 2>/dev/null | grep '\.sql$' || true)
if [ -n "$SQL_DUMPS" ]; then
    echo "Re-importing PostgreSQL dump..."
    # Extract SQL dump to temp location
    TEMP_DIR=$(mktemp -d)
    tar -xzf "$BACKUP_FILE" -C "$TEMP_DIR" --wildcards '*.sql' 2>/dev/null || true
    SQL_FILE=$(find "$TEMP_DIR" -name '*.sql' -type f | head -1)
    if [ -n "$SQL_FILE" ] && [ -s "$SQL_FILE" ]; then
        if docker compose exec -T db \
            psql -U "${DB_USER:-sentinel}" "${DB_NAME:-sentinel}" < "$SQL_FILE" > /dev/null 2>&1; then
            echo "  Database import: OK"
        else
            echo "  Warning: Database import failed (schema may already exist)"
        fi
    fi
    rm -rf "$TEMP_DIR"
else
    echo "  No SQL dump found in archive, skipping DB import."
fi

# ---------- Health check ----------
echo "Verifying restored stack..."
sleep 5
HEALTHY=$(docker compose ps --format '{{.Status}}' 2>/dev/null | grep -c 'healthy' || true)
TOTAL=$(docker compose ps --format '{{.Name}}' 2>/dev/null | wc -l || echo 0)
echo "  Containers: $HEALTHY/$TOTAL healthy"

echo ""
echo "=== Restore Complete ==="
echo "Rollback snapshot saved at: $ROLLBACK_FILE"
echo "To rollback: $0 $ROLLBACK_FILE"
echo "Stack is running. Check status with: docker compose ps"
