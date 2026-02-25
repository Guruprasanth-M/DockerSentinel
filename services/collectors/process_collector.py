"""Polls running processes via psutil."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Dict, List, Set, Tuple

import psutil
import structlog
from redis.asyncio import Redis

from models import ProcessEvent

log = structlog.get_logger()

# Suspicious parent-child patterns (parent → child that's unusual)
SUSPICIOUS_CHILDREN = {
    "nginx": {"bash", "sh", "dash", "python", "python3", "perl", "ruby", "nc", "ncat", "wget", "curl"},
    "apache2": {"bash", "sh", "dash", "python", "python3", "perl", "ruby", "nc", "ncat"},
    "httpd": {"bash", "sh", "dash", "python", "python3", "perl", "ruby", "nc", "ncat"},
    "mysql": {"bash", "sh", "dash", "python", "python3"},
    "mysqld": {"bash", "sh", "dash", "python", "python3"},
    "postgres": {"bash", "sh", "dash"},
    "redis-server": {"bash", "sh", "dash"},
}

CPU_SPIKE_THRESHOLD = 80.0
MEMORY_SPIKE_MB = 500.0


class ProcessCollector:
    """Collects process snapshots and detects changes."""

    def __init__(self) -> None:
        self._previous_pids: Set[int] = set()

    def collect_snapshot(self) -> Tuple[List[ProcessEvent], Dict]:
        """Snapshot running processes and diff with previous."""
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        events: List[ProcessEvent] = []
        current_pids: Set[int] = set()
        summary = {
            "total_processes": 0,
            "new_spawns": 0,
            "cpu_spikes": 0,
            "memory_spikes": 0,
            "unusual_children": 0,
        }

        try:
            for proc in psutil.process_iter(
                ["pid", "name", "cmdline", "cpu_percent", "memory_info",
                 "net_connections", "status", "ppid"]
            ):
                try:
                    info = proc.info
                    pid = info["pid"]
                    current_pids.add(pid)
                    summary["total_processes"] += 1

                    name = info.get("name", "") or ""
                    cmdline = " ".join(info.get("cmdline") or []) or name
                    cpu_pct = info.get("cpu_percent", 0.0) or 0.0
                    mem_info = info.get("memory_info")
                    mem_mb = (mem_info.rss / 1024 / 1024) if mem_info else 0.0
                    conns = info.get("net_connections") or info.get("connections") or []
                    status = info.get("status", "unknown") or "unknown"
                    ppid = info.get("ppid", 0) or 0

                    # Get parent name
                    parent_name = ""
                    try:
                        if ppid > 0:
                            parent = psutil.Process(ppid)
                            parent_name = parent.name()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass

                    # Detect anomalies
                    risk_flag = False
                    risk_reason = None

                    # CPU spike
                    if cpu_pct > CPU_SPIKE_THRESHOLD:
                        risk_flag = True
                        risk_reason = f"High CPU: {cpu_pct:.1f}%"
                        summary["cpu_spikes"] += 1

                    # Memory spike
                    if mem_mb > MEMORY_SPIKE_MB:
                        risk_flag = True
                        risk_reason = f"High memory: {mem_mb:.0f}MB"
                        summary["memory_spikes"] += 1

                    # Unusual child process
                    if parent_name in SUSPICIOUS_CHILDREN:
                        if name in SUSPICIOUS_CHILDREN[parent_name]:
                            risk_flag = True
                            risk_reason = f"Unusual child: {parent_name} → {name}"
                            summary["unusual_children"] += 1

                    # New process spawn
                    is_new = pid not in self._previous_pids
                    if is_new:
                        summary["new_spawns"] += 1

                    event = ProcessEvent(
                        timestamp=now,
                        type="new_process" if is_new else "process_snapshot",
                        pid=pid,
                        name=name,
                        cmdline=cmdline[:500],
                        cpu_percent=cpu_pct,
                        memory_mb=round(mem_mb, 1),
                        connections=len(conns),
                        status=status,
                        ppid=ppid,
                        parent_name=parent_name,
                        risk_flag=risk_flag,
                        risk_reason=risk_reason,
                    )

                    # Only emit events for new/flagged processes (not all snapshots)
                    if is_new or risk_flag:
                        events.append(event)

                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue

        except Exception as e:
            log.error("process_snapshot_error", error=str(e))

        self._previous_pids = current_pids
        return events, summary


async def run(redis: Redis, interval_ms: int = 500) -> None:
    """Run the process collector continuously."""
    collector = ProcessCollector()
    stream_name = "sentinel:processes"
    maxlen = 50000
    interval = interval_ms / 1000.0

    log.info("process_collector_start", interval_ms=interval_ms)

    # Initial CPU measurement (psutil needs two calls for accurate CPU %)
    for proc in psutil.process_iter(["cpu_percent"]):
        pass
    await asyncio.sleep(1)

    while True:
        try:
            events, summary = collector.collect_snapshot()
            for event in events:
                await redis.xadd(
                    stream_name,
                    {"data": event.model_dump_json()},
                    maxlen=maxlen,
                    approximate=True,
                )

            # Publish summary as a separate event for metrics
            await redis.xadd(
                stream_name,
                {"data": ProcessEvent(
                    timestamp=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    type="process_summary",
                    pid=0,
                    name="summary",
                    cmdline=f"total={summary['total_processes']} new={summary['new_spawns']} cpu_spikes={summary['cpu_spikes']}",
                ).model_dump_json()},
                maxlen=maxlen,
                approximate=True,
            )

            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("process_collector_error", error=str(e))
            await asyncio.sleep(2)


# Import for the summary timestamp
from datetime import datetime
