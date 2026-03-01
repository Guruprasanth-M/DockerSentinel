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

# Stop stack
echo "Stopping stack..."
cd "$PROJECT_DIR"
docker compose down 2>/dev/null || true

# Restore data (strip leading directory prefix added during backup)
echo "Restoring data..."
tar -xzf "$BACKUP_FILE" -C "$PROJECT_DIR" --strip-components=1

# Fix permissions
chmod 700 data/redis/ 2>/dev/null || true

# Restart stack
echo "Restarting stack..."
docker compose up -d

echo "=== Restore Complete ==="
# TODO: Verify restored data integrity (checksums, DB consistency check)
# TODO: Re-import PostgreSQL dump if present in the backup archive
# TODO: Add rollback capability if restore fails midway
echo "Stack is starting. Check status with: docker compose ps"
