"""Pydantic models for collector events and state."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class LogLevel(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class LogEvent(BaseModel):
    """A parsed log event from a host log file."""

    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
    source: str
    level: LogLevel = LogLevel.INFO
    type: str = "unknown"
    message: str = ""
    source_ip: Optional[str] = None
    user: Optional[str] = None


class ConnectionState(str, Enum):
    ESTABLISHED = "ESTABLISHED"
    SYN_SENT = "SYN_SENT"
    SYN_RECV = "SYN_RECV"
    FIN_WAIT1 = "FIN_WAIT1"
    FIN_WAIT2 = "FIN_WAIT2"
    TIME_WAIT = "TIME_WAIT"
    CLOSE = "CLOSE"
    CLOSE_WAIT = "CLOSE_WAIT"
    LAST_ACK = "LAST_ACK"
    LISTEN = "LISTEN"
    CLOSING = "CLOSING"
    UNKNOWN = "UNKNOWN"


class NetworkEvent(BaseModel):
    """A network connection event."""

    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
    type: str = "new_connection"
    source_ip: str = ""
    source_port: int = 0
    dest_ip: str = ""
    dest_port: int = 0
    state: str = "UNKNOWN"
    protocol: str = "tcp"


class ProcessEvent(BaseModel):
    """A process snapshot event."""

    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
    type: str = "process_snapshot"
    pid: int
    name: str
    cmdline: str = ""
    cpu_percent: float = 0.0
    memory_mb: float = 0.0
    connections: int = 0
    status: str = "running"
    ppid: int = 0
    parent_name: str = ""
    risk_flag: bool = False
    risk_reason: Optional[str] = None


class FeatureVector(BaseModel):
    """Aggregated features from a 5-second window."""

    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
    window_seconds: int = 5

    # Log features
    log_total_events: int = 0
    log_auth_failures: int = 0
    log_sudo_attempts: int = 0
    log_service_restarts: int = 0

    # Network features
    net_new_connections: int = 0
    net_unique_ips: int = 0
    net_port_scan_candidates: int = 0
    net_bytes_rate: float = 0.0

    # Process features
    proc_new_spawns: int = 0
    proc_cpu_spikes: int = 0
    proc_memory_spikes: int = 0
    proc_unusual_children: int = 0
