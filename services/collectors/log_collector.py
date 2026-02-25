"""Tails host log files into Redis streams."""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import List, Optional

import structlog
from redis.asyncio import Redis

from models import LogEvent, LogLevel
from state import CollectorState

log = structlog.get_logger()

# Host logs directory (mounted read-only)
HOST_LOGS_PATH = os.environ.get("HOST_LOGS_PATH", "/host_logs")

# Log files to monitor (auto-detected)
LOG_PATTERNS = [
    "auth.log",
    "syslog",
    "kern.log",
    "nginx/access.log",
    "nginx/error.log",
    "apache2/access.log",
    "apache2/error.log",
    "mysql/error.log",
    "postgresql/postgresql-*.log",
]

# Regex patterns for log parsing
SSH_FAILURE_RE = re.compile(
    r"Failed password for (?:invalid user )?(\S+) from (\S+) port (\d+)"
)
SSH_SUCCESS_RE = re.compile(
    r"Accepted (?:password|publickey) for (\S+) from (\S+) port (\d+)"
)
SUDO_RE = re.compile(r"sudo:\s+(\S+)\s+:")
SERVICE_RESTART_RE = re.compile(r"systemd\[\d+\]:\s+(Started|Stopped|Restarting)\s+(.+)")
PAM_RE = re.compile(r"pam_unix\(.+\):\s+(authentication failure|session (?:opened|closed))")
KERNEL_OOM_RE = re.compile(r"Out of memory: Kill(?:ed)? process (\d+)")


def discover_log_files() -> List[str]:
    """Discover log files that exist on the host."""
    found: List[str] = []
    base = Path(HOST_LOGS_PATH)

    if not base.exists():
        log.warning("host_logs_not_found", path=HOST_LOGS_PATH)
        return found

    for pattern in LOG_PATTERNS:
        if "*" in pattern:
            for match in base.glob(pattern):
                if match.is_file():
                    found.append(str(match))
        else:
            path = base / pattern
            if path.is_file():
                found.append(str(path))

    log.info("log_files_discovered", count=len(found), files=[os.path.basename(f) for f in found])
    return found


def parse_log_line(line: str, source: str) -> Optional[LogEvent]:
    """Parse a single log line into a structured event."""
    line = line.strip()
    if not line:
        return None

    source_name = os.path.basename(source)
    event = LogEvent(source=source_name, message=line)

    # SSH failure
    match = SSH_FAILURE_RE.search(line)
    if match:
        event.type = "ssh_failure"
        event.level = LogLevel.WARNING
        event.user = match.group(1)
        event.source_ip = match.group(2)
        return event

    # SSH success
    match = SSH_SUCCESS_RE.search(line)
    if match:
        event.type = "ssh_success"
        event.level = LogLevel.INFO
        event.user = match.group(1)
        event.source_ip = match.group(2)
        return event

    # Sudo
    match = SUDO_RE.search(line)
    if match:
        event.type = "sudo_attempt"
        event.level = LogLevel.INFO
        event.user = match.group(1)
        return event

    # Service restart
    match = SERVICE_RESTART_RE.search(line)
    if match:
        event.type = "service_restart"
        event.level = LogLevel.INFO
        return event

    # PAM authentication
    match = PAM_RE.search(line)
    if match:
        if "failure" in match.group(1):
            event.type = "auth_failure"
            event.level = LogLevel.WARNING
        else:
            event.type = "pam_session"
            event.level = LogLevel.INFO
        return event

    # Kernel OOM
    match = KERNEL_OOM_RE.search(line)
    if match:
        event.type = "oom_kill"
        event.level = LogLevel.CRITICAL
        return event

    # nginx/apache access log pattern (basic detection)
    if source_name in ("access.log",) and re.search(r'\d+\.\d+\.\d+\.\d+', line):
        ip_match = re.search(r'(\d+\.\d+\.\d+\.\d+)', line)
        if ip_match:
            event.source_ip = ip_match.group(1)
        # Check for error status codes
        status_match = re.search(r'" (\d{3}) ', line)
        if status_match:
            status = int(status_match.group(1))
            if status >= 500:
                event.level = LogLevel.ERROR
                event.type = "http_5xx"
            elif status >= 400:
                event.level = LogLevel.WARNING
                event.type = "http_4xx"
            else:
                event.type = "http_request"
        return event

    # Default — generic log line
    if any(kw in line.lower() for kw in ("error", "fail", "denied", "refused")):
        event.level = LogLevel.WARNING
        event.type = "error_keyword"

    return event


async def tail_log_file(
    filepath: str,
    redis: Redis,
    state: CollectorState,
    stream_name: str = "sentinel:logs",
    maxlen: int = 50000,
) -> None:
    """Continuously tail a log file and push events to Redis.

    Supports resume-on-restart via CollectorState.
    Handles log rotation (inode change).
    """
    source = os.path.basename(filepath)
    log.info("log_collector_start", file=source, path=filepath)

    while True:
        try:
            if not os.path.exists(filepath):
                log.debug("log_file_missing", file=source)
                await asyncio.sleep(5)
                continue

            stat = os.stat(filepath)
            current_inode = stat.st_ino

            # Check if file was rotated (inode changed)
            saved = state.get_position(filepath)
            if saved and saved.inode != current_inode:
                log.info("log_rotation_detected", file=source)
                state.reset(filepath)
                saved = None

            offset = saved.offset if saved else 0

            with open(filepath, "r", errors="replace") as f:
                # Seek to saved position
                if offset > 0:
                    try:
                        f.seek(offset)
                    except OSError:
                        f.seek(0)

                while True:
                    line = f.readline()
                    if not line:
                        # Save position and wait for new data
                        current_pos = f.tell()
                        state.set_position(filepath, current_inode, current_pos)
                        await asyncio.sleep(0.5)
                        continue

                    event = parse_log_line(line, filepath)
                    if event:
                        try:
                            await redis.xadd(
                                stream_name,
                                {"data": event.model_dump_json()},
                                maxlen=maxlen,
                                approximate=True,
                            )
                        except Exception as e:
                            log.error("redis_xadd_error", error=str(e), file=source)
                            await asyncio.sleep(1)

        except asyncio.CancelledError:
            # Save state before shutdown
            state.save()
            raise
        except Exception as e:
            log.error("log_collector_error", file=source, error=str(e))
            await asyncio.sleep(2)
