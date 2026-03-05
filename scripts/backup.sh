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

# Generate SHA256 checksum for integrity verification
echo "Generating checksum..."
CHECKSUM_FILE="${BACKUP_FILE}.sha256"
sha256sum "$BACKUP_FILE" > "$CHECKSUM_FILE"
echo "  Checksum: $(cat "$CHECKSUM_FILE" | cut -d' ' -f1)"

# Verify archive integrity
echo "Verifying archive..."
if tar -tzf "$BACKUP_FILE" > /dev/null 2>&1; then
    echo "  Archive integrity: OK"
else
    echo "  WARNING: Archive integrity check FAILED"
fi

# Backup rotation — keep last 10 backups
BACKUP_COUNT=$(ls -1 "$BACKUP_DIR"/sentinel_backup_*.tar.gz 2>/dev/null | wc -l)
if [ "$BACKUP_COUNT" -gt 10 ]; then
    REMOVE_COUNT=$((BACKUP_COUNT - 10))
    echo "Rotating backups (removing $REMOVE_COUNT oldest)..."
    ls -1t "$BACKUP_DIR"/sentinel_backup_*.tar.gz | tail -n "$REMOVE_COUNT" | while read -r old_backup; do
        rm -f "$old_backup" "${old_backup}.sha256"
        echo "  Removed: $(basename "$old_backup")"
    done
fi

echo "=== Backup Complete ==="
echo "File: $BACKUP_FILE"
echo "Size: $BACKUP_SIZE"
echo "Checksum: $CHECKSUM_FILE"
