"""Parses /proc/net/tcp to track connections."""

from __future__ import annotations

import asyncio
import os
import re
import struct
import socket
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Set, Tuple

import structlog
from redis.asyncio import Redis

from models import NetworkEvent

log = structlog.get_logger()

# TCP states from kernel (hex value → name)
TCP_STATES = {
    "01": "ESTABLISHED",
    "02": "SYN_SENT",
    "03": "SYN_RECV",
    "04": "FIN_WAIT1",
    "05": "FIN_WAIT2",
    "06": "TIME_WAIT",
    "07": "CLOSE",
    "08": "CLOSE_WAIT",
    "09": "LAST_ACK",
    "0A": "LISTEN",
    "0B": "CLOSING",
}


def _hex_to_ip(hex_ip: str) -> str:
    """Convert hex-encoded IP address to dotted decimal."""
    try:
        ip_int = int(hex_ip, 16)
        ip_bytes = struct.pack("<I", ip_int)
        return socket.inet_ntoa(ip_bytes)
    except Exception:
        return "0.0.0.0"


def _hex_to_port(hex_port: str) -> int:
    """Convert hex-encoded port to integer."""
    try:
        return int(hex_port, 16)
    except Exception:
        return 0


def parse_proc_net_tcp(content: str) -> List[Dict]:
    """Parse /proc/net/tcp content into connection dictionaries."""
    connections = []

    for line in content.strip().split("\n")[1:]:  # Skip header
        line = line.strip()
        if not line:
            continue

        parts = line.split()
        if len(parts) < 4:
            continue

        try:
            # Parse local address
            local_parts = parts[1].split(":")
            local_ip = _hex_to_ip(local_parts[0])
            local_port = _hex_to_port(local_parts[1])

            # Parse remote address
            remote_parts = parts[2].split(":")
            remote_ip = _hex_to_ip(remote_parts[0])
            remote_port = _hex_to_port(remote_parts[1])

            # Parse state
            state_hex = parts[3]
            state = TCP_STATES.get(state_hex, "UNKNOWN")

            connections.append({
                "local_ip": local_ip,
                "local_port": local_port,
                "remote_ip": remote_ip,
                "remote_port": remote_port,
                "state": state,
            })
        except (IndexError, ValueError) as e:
            continue

    return connections


def _connection_key(conn: Dict) -> str:
    """Generate a unique key for a connection."""
    return f"{conn['remote_ip']}:{conn['remote_port']}->{conn['local_ip']}:{conn['local_port']}"


class NetworkCollector:
    """Collects TCP connection data from /proc/net/tcp."""

    def __init__(self) -> None:
        self._previous_connections: Set[str] = set()
        self._ip_port_tracker: Dict[str, Set[int]] = defaultdict(set)
        self._ip_port_window_start: float = 0.0

    def _read_proc_net_tcp(self) -> str:
        """Read /proc/net/tcp content."""
        tcp_path = "/proc/net/tcp"
        try:
            # Try host /proc first (if mounted), fall back to container /proc
            if os.path.exists("/host_proc/net/tcp"):
                tcp_path = "/host_proc/net/tcp"
            with open(tcp_path, "r") as f:
                return f.read()
        except Exception as e:
            log.error("proc_net_tcp_read_error", error=str(e))
            return ""

    def collect_snapshot(self) -> Tuple[List[NetworkEvent], List[Dict]]:
        """Take a connection snapshot, diff against previous, emit events."""
        content = self._read_proc_net_tcp()
        if not content:
            return [], []

        connections = parse_proc_net_tcp(content)
        current_keys = {_connection_key(c) for c in connections}
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        events: List[NetworkEvent] = []

        # Find new connections
        new_keys = current_keys - self._previous_connections
        for conn in connections:
            key = _connection_key(conn)
            if key in new_keys and conn["remote_ip"] != "0.0.0.0":
                event = NetworkEvent(
                    timestamp=now,
                    type="new_connection",
                    source_ip=conn["remote_ip"],
                    source_port=conn["remote_port"],
                    dest_ip=conn["local_ip"],
                    dest_port=conn["local_port"],
                    state=conn["state"],
                )
                events.append(event)

                # Track for port scan detection
                self._ip_port_tracker[conn["remote_ip"]].add(conn["local_port"])

        # Find closed connections
        closed_keys = self._previous_connections - current_keys
        if len(closed_keys) > 0:
            events.append(NetworkEvent(
                timestamp=now,
                type="connections_closed",
                source_ip="summary",
                dest_port=len(closed_keys),
            ))

        # Port scan detection: >5 ports from single IP in 30s window
        try:
            current_time = asyncio.get_running_loop().time()
        except RuntimeError:
            current_time = 0
        if current_time - self._ip_port_window_start > 30:
            self._ip_port_tracker.clear()
            self._ip_port_window_start = current_time

        for ip, ports in self._ip_port_tracker.items():
            if len(ports) > 5:
                events.append(NetworkEvent(
                    timestamp=now,
                    type="port_scan_candidate",
                    source_ip=ip,
                    dest_port=len(ports),
                ))

        self._previous_connections = current_keys
        return events, connections


async def run(redis: Redis, interval_ms: int = 500) -> None:
    """Run the network collector continuously."""
    collector = NetworkCollector()
    stream_name = "hostspectra:network"
    maxlen = 50000
    interval = interval_ms / 1000.0

    log.info("network_collector_start", interval_ms=interval_ms)

    while True:
        try:
            events, _ = collector.collect_snapshot()
            for event in events:
                await redis.xadd(
                    stream_name,
                    {"data": event.model_dump_json()},
                    maxlen=maxlen,
                    approximate=True,
                )
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("network_collector_error", error=str(e))
            await asyncio.sleep(2)
