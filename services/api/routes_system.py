"""Process and port endpoints."""

from __future__ import annotations

import os
from collections import defaultdict
from typing import Dict, List, Set

import psutil
import structlog
from fastapi import APIRouter, Query, Request
from redis.asyncio import Redis

from schemas import (
    PortInfo,
    PortsResponse,
    ProcessInfo,
    ProcessesResponse,
)

log = structlog.get_logger()

router = APIRouter()

# Service port hints
PORT_HINTS = {
    22: "SSH", 80: "HTTP", 443: "HTTPS", 3306: "MySQL",
    5432: "PostgreSQL", 6379: "Redis", 8080: "HTTP-Alt",
    8000: "API", 8443: "HTTPS-Alt", 27017: "MongoDB",
    9200: "Elasticsearch", 5601: "Kibana", 3000: "Grafana",
}


def get_redis(request) -> Redis:
    """Get Redis client from app state."""
    return request.app.state.redis


@router.get("/processes", response_model=ProcessesResponse)
async def processes(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    sort: str = Query(default="cpu", pattern="^(cpu|memory|connections)$"),
    flagged_only: bool = Query(default=False),
):
    """List running processes from the host system.

    Uses /host_proc if available (bind-mounted from host), otherwise
    falls back to container-local psutil.
    """
    process_list: List[ProcessInfo] = []

    # Use host /proc if mounted, otherwise use container-local psutil
    host_proc = "/host_proc"
    use_host = os.path.isdir(host_proc)

    if use_host:
        process_list = _read_host_processes(host_proc, flagged_only)
    else:
        process_list = _read_local_processes(flagged_only)

    # Sort
    if sort == "cpu":
        process_list.sort(key=lambda p: p.cpu_percent, reverse=True)
    elif sort == "memory":
        process_list.sort(key=lambda p: p.memory_mb, reverse=True)
    elif sort == "connections":
        process_list.sort(key=lambda p: p.connections, reverse=True)

    total = len(process_list)
    return ProcessesResponse(processes=process_list[:limit], total=total)


def _get_host_uptime(host_proc: str) -> float:
    """Read system uptime in seconds from /proc/uptime."""
    try:
        uptime_path = os.path.join(host_proc, "uptime")
        with open(uptime_path, "r") as f:
            return float(f.read().split()[0])
    except Exception:
        return 1.0


def _get_clk_tck() -> int:
    """Get clock ticks per second (usually 100 on Linux)."""
    try:
        return os.sysconf("SC_CLK_TCK")
    except (AttributeError, ValueError):
        return 100


def _read_host_processes(host_proc: str, flagged_only: bool) -> List[ProcessInfo]:
    """Read processes from host /proc filesystem."""
    process_list: List[ProcessInfo] = []
    clk_tck = _get_clk_tck()
    num_cpus = os.cpu_count() or 1
    system_uptime = _get_host_uptime(host_proc)

    try:
        for entry in os.listdir(host_proc):
            if not entry.isdigit():
                continue
            pid = int(entry)
            try:
                stat_path = os.path.join(host_proc, entry, "stat")
                status_path = os.path.join(host_proc, entry, "status")
                cmdline_path = os.path.join(host_proc, entry, "cmdline")

                if not os.path.exists(stat_path):
                    continue

                name = ""
                mem_kb = 0
                status = "running"
                with open(status_path, "r") as f:
                    for line in f:
                        if line.startswith("Name:"):
                            name = line.split(":", 1)[1].strip()
                        elif line.startswith("VmRSS:"):
                            try:
                                mem_kb = int(line.split()[1])
                            except (IndexError, ValueError):
                                pass
                        elif line.startswith("State:"):
                            state_char = line.split()[1]
                            status = {
                                "R": "running", "S": "sleeping", "D": "disk-sleep",
                                "Z": "zombie", "T": "stopped", "t": "tracing-stop",
                                "X": "dead", "I": "idle",
                            }.get(state_char, "unknown")

                mem_mb = mem_kb / 1024.0

                cmdline = ""
                try:
                    with open(cmdline_path, "r") as f:
                        cmdline = f.read().replace("\0", " ").strip()
                except Exception:
                    pass

                cpu_pct = 0.0
                try:
                    with open(stat_path, "r") as f:
                        parts = f.read().split()
                    if len(parts) > 21:
                        utime = int(parts[13])
                        stime = int(parts[14])
                        starttime = int(parts[21])
                        total_cpu_seconds = (utime + stime) / clk_tck
                        proc_uptime = system_uptime - (starttime / clk_tck)
                        if proc_uptime > 0:
                            cpu_pct = (total_cpu_seconds / proc_uptime) * 100.0 / num_cpus
                            cpu_pct = min(100.0, max(0.0, cpu_pct))
                except Exception:
                    pass

                risk_flag = False
                risk_reason = None
                if cpu_pct > 80:
                    risk_flag = True
                    risk_reason = f"High CPU: {cpu_pct:.1f}%"
                elif mem_mb > 500:
                    risk_flag = True
                    risk_reason = f"High memory: {mem_mb:.0f}MB"

                if flagged_only and not risk_flag:
                    continue

                process_list.append(ProcessInfo(
                    pid=pid,
                    name=name,
                    cmdline=cmdline[:200],
                    cpu_percent=round(cpu_pct, 1),
                    memory_mb=round(mem_mb, 1),
                    connections=0,
                    status=status,
                    risk_flag=risk_flag,
                    risk_reason=risk_reason,
                ))
            except (PermissionError, FileNotFoundError, ProcessLookupError):
                continue
    except Exception as e:
        log.error("host_proc_read_error", error=str(e))
    return process_list


def _read_local_processes(flagged_only: bool) -> List[ProcessInfo]:
    """Read processes from container-local psutil."""
    process_list: List[ProcessInfo] = []
    for proc in psutil.process_iter(
        ["pid", "name", "cmdline", "cpu_percent", "memory_info", "net_connections", "status"]
    ):
        try:
            info = proc.info
            mem_info = info.get("memory_info")
            mem_mb = (mem_info.rss / 1024 / 1024) if mem_info else 0.0
            conns = info.get("net_connections") or info.get("connections") or []
            cpu_pct = info.get("cpu_percent", 0.0) or 0.0

            risk_flag = False
            risk_reason = None
            if cpu_pct > 80:
                risk_flag = True
                risk_reason = f"High CPU: {cpu_pct:.1f}%"
            elif mem_mb > 500:
                risk_flag = True
                risk_reason = f"High memory: {mem_mb:.0f}MB"

            if flagged_only and not risk_flag:
                continue

            process_list.append(ProcessInfo(
                pid=info["pid"],
                name=info.get("name", "") or "",
                cmdline=" ".join(info.get("cmdline") or []) or "",
                cpu_percent=round(cpu_pct, 1),
                memory_mb=round(mem_mb, 1),
                connections=len(conns),
                status=info.get("status", "unknown") or "unknown",
                risk_flag=risk_flag,
                risk_reason=risk_reason,
            ))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return process_list


@router.get("/ports", response_model=PortsResponse)
async def ports(request: Request):
    """Active ports and connection statistics (C5: reads from /host_proc)."""
    port_data: Dict[int, Dict] = defaultdict(lambda: {
        "connections": 0,
        "unique_ips": set(),
        "states": [],
    })

    host_proc = "/host_proc"
    use_host = os.path.isdir(host_proc)

    if use_host:
        # C5: Read from host /proc/net/tcp and /proc/net/tcp6
        for proto_file in ["net/tcp", "net/tcp6"]:
            try:
                filepath = os.path.join(host_proc, proto_file)
                with open(filepath, "r") as f:
                    lines = f.readlines()[1:]  # skip header
                for line in lines:
                    parts = line.strip().split()
                    if len(parts) < 4:
                        continue
                    local_addr = parts[1]
                    remote_addr = parts[2]
                    state_hex = parts[3]

                    # Parse local port (hex)
                    local_port = int(local_addr.split(":")[1], 16)

                    # Parse remote IP (hex to dotted quad for IPv4)
                    remote_parts = remote_addr.split(":")
                    remote_ip_hex = remote_parts[0]
                    if len(remote_ip_hex) == 8:  # IPv4
                        ip_int = int(remote_ip_hex, 16)
                        remote_ip = f"{ip_int & 0xFF}.{(ip_int >> 8) & 0xFF}.{(ip_int >> 16) & 0xFF}.{(ip_int >> 24) & 0xFF}"
                    else:
                        remote_ip = remote_ip_hex  # IPv6 left as hex

                    # Map state
                    state_map = {
                        "01": "ESTABLISHED", "02": "SYN_SENT", "03": "SYN_RECV",
                        "04": "FIN_WAIT1", "05": "FIN_WAIT2", "06": "TIME_WAIT",
                        "07": "CLOSE", "08": "CLOSE_WAIT", "09": "LAST_ACK",
                        "0A": "LISTEN", "0B": "CLOSING",
                    }
                    state = state_map.get(state_hex, "UNKNOWN")

                    if state == "LISTEN" or state == "ESTABLISHED":
                        port_data[local_port]["connections"] += 1
                        if remote_ip != "0.0.0.0" and remote_ip != "00000000":
                            port_data[local_port]["unique_ips"].add(remote_ip)
                        port_data[local_port]["states"].append(state)
            except Exception as e:
                log.debug("host_proc_net_read_error", file=proto_file, error=str(e))
    else:
        # Fallback to container psutil
        for conn in psutil.net_connections(kind="tcp"):
            if conn.laddr:
                port = conn.laddr.port
                port_data[port]["connections"] += 1
                if conn.raddr:
                    port_data[port]["unique_ips"].add(conn.raddr.ip)
                port_data[port]["states"].append(conn.status)

    port_list: List[PortInfo] = []
    for port, data in sorted(port_data.items()):
        unique_ips = len(data["unique_ips"])
        risk_flag = unique_ips > 10

        port_list.append(PortInfo(
            port=port,
            protocol="tcp",
            connections_per_minute=data["connections"],
            unique_ips_1min=unique_ips,
            state="normal" if not risk_flag else "elevated",
            risk_flag=risk_flag,
            service_hint=PORT_HINTS.get(port, ""),
        ))

    return PortsResponse(ports=port_list)
