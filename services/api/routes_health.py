"""Health and metrics endpoints."""

from __future__ import annotations

import asyncio
import json
import os
import time

import psutil
import structlog
from fastapi import APIRouter, Request
from redis.asyncio import Redis

from schemas import HealthResponse, MetricsResponse, ServiceHealth, StatusResponse

log = structlog.get_logger()

router = APIRouter()

HOST_PROC = "/host_proc"

# M13: Read host boot time from /host_proc instead of container boot time
def _get_host_boot_time() -> float:
    """Get real host boot time from /host_proc/stat."""
    try:
        with open(os.path.join(HOST_PROC, "stat"), "r") as f:
            for line in f:
                if line.startswith("btime "):
                    return float(line.strip().split()[1])
    except Exception:
        pass
    return psutil.boot_time()

_boot_time = _get_host_boot_time()

_prev_net_io = None
_prev_net_time = None
_prev_cpu_times = None
_prev_cpu_time_ts = None


def _read_host_net_io():
    """Read network bytes from /host_proc/net/dev for real host traffic."""
    net_dev_path = os.path.join(HOST_PROC, "net", "dev")
    total_rx = 0
    total_tx = 0

    try:
        with open(net_dev_path, "r") as f:
            lines = f.readlines()[2:]
        for line in lines:
            parts = line.strip().split()
            if len(parts) < 10:
                continue
            iface = parts[0].rstrip(":")
            if iface == "lo":
                continue
            total_rx += int(parts[1])
            total_tx += int(parts[9])
    except Exception:
        counters = psutil.net_io_counters()
        total_rx = counters.bytes_recv
        total_tx = counters.bytes_sent

    return total_rx, total_tx


def _read_host_cpu_percent() -> float:
    """C3: Read CPU usage from /host_proc/stat instead of container psutil."""
    global _prev_cpu_times, _prev_cpu_time_ts
    try:
        with open(os.path.join(HOST_PROC, "stat"), "r") as f:
            line = f.readline()
        parts = line.split()
        # user, nice, system, idle, iowait, irq, softirq, steal
        times = [int(x) for x in parts[1:9]]
        total = sum(times)
        idle = times[3] + times[4]  # idle + iowait

        now = time.time()
        if _prev_cpu_times is not None:
            prev_total = sum(_prev_cpu_times)
            prev_idle = _prev_cpu_times[3] + _prev_cpu_times[4]
            total_diff = total - prev_total
            idle_diff = idle - prev_idle
            if total_diff > 0:
                cpu_percent = ((total_diff - idle_diff) / total_diff) * 100.0
            else:
                cpu_percent = 0.0
        else:
            cpu_percent = 0.0

        _prev_cpu_times = times
        _prev_cpu_time_ts = now
        return round(cpu_percent, 1)
    except Exception:
        return psutil.cpu_percent(interval=0)


def _read_host_memory() -> dict:
    """C3: Read memory from /host_proc/meminfo instead of container psutil."""
    meminfo = {}
    try:
        with open(os.path.join(HOST_PROC, "meminfo"), "r") as f:
            for line in f:
                parts = line.split()
                key = parts[0].rstrip(":")
                val = int(parts[1]) * 1024  # Convert kB to bytes
                meminfo[key] = val

        total = meminfo.get("MemTotal", 0)
        available = meminfo.get("MemAvailable", 0)
        used = total - available
        percent = (used / total * 100.0) if total > 0 else 0.0

        return {
            "percent": round(percent, 1),
            "used_mb": round(used / (1024 * 1024), 1),
            "total_mb": round(total / (1024 * 1024), 1),
        }
    except Exception:
        mem = psutil.virtual_memory()
        return {
            "percent": round(mem.percent, 1),
            "used_mb": round(mem.used / (1024 * 1024), 1),
            "total_mb": round(mem.total / (1024 * 1024), 1),
        }


def _read_host_connections() -> int:
    """C5: Count active TCP connections from /host_proc/net/tcp."""
    count = 0
    for fname in ["net/tcp", "net/tcp6"]:
        try:
            with open(os.path.join(HOST_PROC, fname), "r") as f:
                lines = f.readlines()[1:]  # skip header
            for line in lines:
                parts = line.strip().split()
                if len(parts) >= 4:
                    state = parts[3]
                    if state == "01":  # ESTABLISHED
                        count += 1
        except Exception:
            pass
    return count


def get_redis(request) -> Redis:
    """Get Redis client from app state."""
    return request.app.state.redis


@router.get("/health", response_model=HealthResponse)
async def health(request: Request):
    """Health check endpoint — no auth required."""
    redis: Redis = get_redis(request)
    uptime = int(time.time() - _boot_time)

    services = ServiceHealth(api="healthy")

    try:
        await redis.ping()
        services.redis = "connected"
    except Exception:
        services.redis = "disconnected"

    # Check DB health via TCP connect to postgres
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection("db", 5432), timeout=2.0
        )
        writer.close()
        await writer.wait_closed()
        services.db = "connected"
    except Exception:
        services.db = "disconnected"

    # Check collectors health via heartbeat key
    try:
        collectors_ts = await redis.get("hostspectra:heartbeat:collectors")
        if collectors_ts:
            services.collectors = "running"
        else:
            services.collectors = "inactive"
    except Exception:
        services.collectors = "unknown"

    # Check ML health via heartbeat key (TTL-based: expires ~20s after ML stops)
    try:
        ml_ts = await redis.get("hostspectra:heartbeat:ml")
        if ml_ts:
            services.ml_engine = "active"
        else:
            services.ml_engine = "inactive"
    except Exception:
        services.ml_engine = "unknown"

    # Check policy engine
    try:
        policy_ts = await redis.get("hostspectra:heartbeat:policy_engine")
        services.policy_engine = "active" if policy_ts else "inactive"
    except Exception:
        services.policy_engine = "unknown"

    # Check action engine
    try:
        action_ts = await redis.get("hostspectra:heartbeat:action_engine")
        services.action_engine = "active" if action_ts else "inactive"
    except Exception:
        services.action_engine = "unknown"

    # Check webhook service
    try:
        webhook_ts = await redis.get("hostspectra:heartbeat:webhook_service")
        services.webhook_service = "active" if webhook_ts else "inactive"
    except Exception:
        services.webhook_service = "unknown"

    return HealthResponse(
        status="healthy" if services.redis == "connected" else "degraded",
        version="v0.2",
        services=services,
        uptime_seconds=uptime,
    )


@router.get("/metrics", response_model=MetricsResponse)
async def metrics(request: Request):
    """Current system metrics — reads from host /proc, not container psutil (C3)."""
    redis: Redis = get_redis(request)

    global _prev_net_io, _prev_net_time

    # C3: Read host CPU from /host_proc/stat
    cpu_percent = _read_host_cpu_percent()

    # C3: Read host memory from /host_proc/meminfo
    memory = _read_host_memory()

    host_rx, host_tx = _read_host_net_io()

    now_ts = time.time()
    net_sent_rate = 0.0
    net_recv_rate = 0.0
    if _prev_net_io is not None and _prev_net_time is not None:
        elapsed = now_ts - _prev_net_time
        if elapsed > 0:
            net_sent_rate = (host_tx - _prev_net_io[1]) / elapsed
            net_recv_rate = (host_rx - _prev_net_io[0]) / elapsed
    _prev_net_io = (host_rx, host_tx)
    _prev_net_time = now_ts

    # Disk usage (from host root via /host_proc if possible)
    try:
        # Try reading host disk from /host_proc/mounts filesystem
        disk = psutil.disk_usage("/")
        disk_percent = round(disk.percent, 1)
        disk_used_gb = round(disk.used / (1024 ** 3), 2)
        disk_total_gb = round(disk.total / (1024 ** 3), 2)
    except Exception:
        disk_percent = 0.0
        disk_used_gb = 0.0
        disk_total_gb = 0.0

    # Load average from /host_proc/loadavg
    try:
        with open(os.path.join(HOST_PROC, "loadavg"), "r") as f:
            parts = f.read().split()
        load_1, load_5, load_15 = float(parts[0]), float(parts[1]), float(parts[2])
    except Exception:
        try:
            load_1, load_5, load_15 = os.getloadavg()
        except (OSError, AttributeError):
            load_1, load_5, load_15 = 0.0, 0.0, 0.0

    # Get latest risk score from ML
    risk_score = 0.0
    risk_level = "normal"
    try:
        latest = await redis.get("hostspectra:latest_score")
        if latest:
            score_data = json.loads(latest)
            risk_score = score_data.get("score", 0.0)
            risk_level = score_data.get("risk_level", "normal")
    except Exception:
        pass

    # C5: Count active host TCP connections from /host_proc
    active_conns = _read_host_connections()

    # M13: System uptime from host boot time
    uptime = int(time.time() - _boot_time)

    # Anomaly count (24h) from Redis
    anomaly_count = 0
    try:
        anomaly_count = int(await redis.get("hostspectra:anomaly_count_24h") or 0)
    except Exception:
        pass

    # Alert count from alerts stream
    alert_count = 0
    try:
        alert_count = await redis.xlen("hostspectra:alerts")
    except Exception:
        pass

    from datetime import datetime, timezone
    return MetricsResponse(
        timestamp=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        cpu_percent=cpu_percent,
        memory_percent=memory["percent"],
        memory_used_mb=memory["used_mb"],
        memory_total_mb=memory["total_mb"],
        disk_percent=disk_percent,
        disk_used_gb=disk_used_gb,
        disk_total_gb=disk_total_gb,
        load_1m=round(load_1, 2),
        load_5m=round(load_5, 2),
        load_15m=round(load_15, 2),
        network_bytes_sent_per_sec=round(net_sent_rate, 1),
        network_bytes_recv_per_sec=round(net_recv_rate, 1),
        active_connections=active_conns,
        risk_score=round(risk_score, 4),
        risk_level=risk_level,
        uptime_seconds=uptime,
        anomaly_count=anomaly_count,
        alert_count=alert_count,
    )


@router.get("/status", response_model=StatusResponse)
async def status(request: Request):
    """Combined health + metrics in a single request to reduce dashboard overhead."""
    health_data = await health(request)
    metrics_data = await metrics(request)
    return StatusResponse(health=health_data, metrics=metrics_data)
