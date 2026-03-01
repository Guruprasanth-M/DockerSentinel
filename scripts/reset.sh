#!/bin/bash
# reset.sh — Factory reset Docker Sentinel (wipes all data, keeps config)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo "=== Docker Sentinel Factory Reset ==="
echo "This will DELETE all data in ./data/"
echo "Configuration files in ./config/ will be KEPT."
echo ""
read -p "Are you sure? Type 'RESET' to confirm: " -r
echo ""

if [ "$REPLY" != "RESET" ]; then
    echo "Reset cancelled."
    exit 0
fi

# Stop stack
echo "Stopping stack..."
cd "$PROJECT_DIR"
docker compose down 2>/dev/null || true

# Wipe data
echo "Wiping data directories..."
rm -rf "$PROJECT_DIR/data/redis/"*
rm -rf "$PROJECT_DIR/data/models/"*
rm -rf "$PROJECT_DIR/data/audit/"*
rm -rf "$PROJECT_DIR/data/collector-state/"*

# Recreate directories
mkdir -p "$PROJECT_DIR/data/redis"
mkdir -p "$PROJECT_DIR/data/models"
mkdir -p "$PROJECT_DIR/data/audit"
mkdir -p "$PROJECT_DIR/data/collector-state"
chmod 700 "$PROJECT_DIR/data/redis"

# Restart stack
echo "Restarting stack..."
docker compose up -d

echo "=== Reset Complete ==="
# TODO: Also wipe PostgreSQL data (data/db/) for a true factory reset
# TODO: Regenerate secrets (.env) on reset to avoid stale credentials
# TODO: Add --force flag to skip interactive confirmation
echo "System restarted with clean state."
echo "Pre-trained ML model will be regenerated on first start."
