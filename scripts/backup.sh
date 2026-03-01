#!/bin/bash
# backup.sh — Create a backup archive of all Docker Sentinel data
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR="$PROJECT_DIR/backups"
BACKUP_FILE="$BACKUP_DIR/sentinel_backup_${TIMESTAMP}.tar.gz"

mkdir -p "$BACKUP_DIR"

echo "=== Docker Sentinel Backup ==="
echo "Timestamp: $TIMESTAMP"
echo "Backup file: $BACKUP_FILE"

# Dump PostgreSQL database (while still running for consistency)
echo "Dumping PostgreSQL database..."
DB_DUMP="$BACKUP_DIR/db_dump_${TIMESTAMP}.sql"
if docker compose -f "$PROJECT_DIR/docker-compose.yml" exec -T db \
    pg_dump -U "${DB_USER:-sentinel}" "${DB_NAME:-sentinel}" > "$DB_DUMP" 2>/dev/null; then
    echo "  Database dump created ($(du -h "$DB_DUMP" | cut -f1))"
else
    echo "  Warning: Database dump failed (container may not be running)"
    touch "$DB_DUMP"
fi

# Stop stack for clean file snapshot
echo "Stopping stack for clean snapshot..."
cd "$PROJECT_DIR"
docker compose stop 2>/dev/null || true

# Create backup archive
echo "Creating backup archive..."
tar -czf "$BACKUP_FILE" \
    -C "$PROJECT_DIR" \
    data/redis/ \
    data/models/ \
    data/audit/ \
    data/collector-state/ \
    data/db/ \
    config/ \
    --transform="s|^|sentinel_backup_${TIMESTAMP}/|" \
    2>/dev/null || true

# Add DB dump: decompress, append, recompress (tar cannot append to .gz directly)
if [ -s "$DB_DUMP" ]; then
    TEMP_TAR="${BACKUP_FILE%.gz}"
    gunzip -k "$BACKUP_FILE" 2>/dev/null && \
    tar -rf "$TEMP_TAR" -C "$BACKUP_DIR" "db_dump_${TIMESTAMP}.sql" 2>/dev/null && \
    gzip -f "$TEMP_TAR" 2>/dev/null || true
fi
rm -f "$DB_DUMP"

# Restart stack
echo "Restarting stack..."
docker compose up -d

BACKUP_SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
echo "=== Backup Complete ==="
# TODO: Add backup integrity verification (checksum) after archive creation
# TODO: Implement backup rotation — delete backups older than N days
# TODO: Add optional upload to S3/GCS for offsite backup
echo "File: $BACKUP_FILE"
echo "Size: $BACKUP_SIZE"
