#!/bin/bash
# simulate_attack.sh — Inject synthetic attack events into Redis for testing
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

ATTACK_TYPE="${1:-help}"

if [ -f "$PROJECT_DIR/.env" ]; then
    REDIS_PASSWORD=$(grep '^REDIS_PASSWORD=' "$PROJECT_DIR/.env" | cut -d= -f2)
fi
REDIS_PASSWORD="${REDIS_PASSWORD:-hostspectra_redis_pass}"
REDIS_CMD="docker compose exec -T redis redis-cli -a $REDIS_PASSWORD --no-auth-warning"

cd "$PROJECT_DIR"

inject() {
    $REDIS_CMD XADD "$1" '*' data "$2" >/dev/null
}

case "$ATTACK_TYPE" in
    brute_force)
        echo "=== Simulating SSH Brute Force Attack ==="
        for i in $(seq 1 100); do
            T=$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)
            IP="10.0.0.$((RANDOM % 5 + 1))"
            inject "sentinel:logs" "{\"timestamp\":\"$T\",\"source\":\"auth.log\",\"level\":\"warning\",\"type\":\"ssh_failure\",\"message\":\"Failed password for root from $IP\",\"source_ip\":\"$IP\",\"user\":\"root\"}" &
        done
        wait
        echo "  Injected 100 SSH failure events"
        echo "=== Brute Force Simulation Complete ==="
        ;;

    port_scan)
        echo "=== Simulating Port Scan ==="
        SCANNER_IP="10.0.0.99"
        for port in $(seq 1 100); do
            T=$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)
            inject "sentinel:network" "{\"timestamp\":\"$T\",\"type\":\"new_connection\",\"source_ip\":\"$SCANNER_IP\",\"dest_port\":$port,\"state\":\"SYN_RECV\",\"protocol\":\"tcp\"}" &
            if (( port % 10 == 0 )); then
                # Inject port_scan_candidate every 10 connections
                inject "sentinel:network" "{\"timestamp\":\"$T\",\"type\":\"port_scan_candidate\",\"source_ip\":\"$SCANNER_IP\",\"ports_scanned\":$port}" &
            fi
        done
        wait
        echo "  Injected 110 port scan events"
        echo "=== Port Scan Simulation Complete ==="
        ;;

    all)
        echo "=== Running All Attack Simulations ==="
        "$0" brute_force
        "$0" port_scan
        echo "=== All Simulations Complete ==="
        ;;

    help|*)
        echo "Usage: $0 <attack_type>"
        echo ""
        echo "Attack types:"
        echo "  brute_force  — 100 SSH failure events from random IPs"
        echo "  port_scan    — 100 rapid multi-port connection events"
        echo "  all          — Run all attack simulations"
        ;;
esac
