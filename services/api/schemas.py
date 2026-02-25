"""Pydantic models for API responses."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ServiceHealth(BaseModel):
    redis: str = "unknown"
    db: str = "unknown"
    collectors: str = "unknown"
    ml_engine: str = "unknown"
    policy_engine: str = "unknown"
    action_engine: str = "unknown"
    webhook_service: str = "unknown"
    api: str = "healthy"


class HealthResponse(BaseModel):
    status: str = "healthy"
    version: str = "v0.1"
    services: ServiceHealth = Field(default_factory=ServiceHealth)
    uptime_seconds: int = 0
    model_version: str = "v1_pretrained"


class MetricsResponse(BaseModel):
    timestamp: str = ""
    cpu_percent: float = 0.0
    memory_percent: float = 0.0
    memory_used_mb: float = 0.0
    memory_total_mb: float = 0.0
    disk_percent: float = 0.0
    disk_used_gb: float = 0.0
    disk_total_gb: float = 0.0
    load_1m: float = 0.0
    load_5m: float = 0.0
    load_15m: float = 0.0
    network_bytes_sent_per_sec: float = 0.0
    network_bytes_recv_per_sec: float = 0.0
    active_connections: int = 0
    risk_score: float = 0.0
    risk_level: str = "normal"
    uptime_seconds: int = 0
    anomaly_count: int = 0
    alert_count: int = 0


class StatusResponse(BaseModel):
    """Combined health + metrics in a single response to reduce API calls."""
    health: HealthResponse = Field(default_factory=HealthResponse)
    metrics: MetricsResponse = Field(default_factory=MetricsResponse)


class ProcessInfo(BaseModel):
    pid: int
    name: str
    cmdline: str = ""
    cpu_percent: float = 0.0
    memory_mb: float = 0.0
    connections: int = 0
    status: str = "running"
    risk_flag: bool = False
    risk_reason: Optional[str] = None


class ProcessesResponse(BaseModel):
    processes: List[ProcessInfo] = Field(default_factory=list)
    total: int = 0


class PortInfo(BaseModel):
    port: int
    protocol: str = "tcp"
    connections_per_minute: int = 0
    unique_ips_1min: int = 0
    state: str = "normal"
    risk_flag: bool = False
    service_hint: str = ""


class PortsResponse(BaseModel):
    ports: List[PortInfo] = Field(default_factory=list)


class LogEventParsed(BaseModel):
    type: str = "unknown"
    user: Optional[str] = None
    source_ip: Optional[str] = None


class LogEntry(BaseModel):
    id: Optional[str] = None  # M4: stream ID for cursor pagination
    timestamp: str = ""
    source: str = ""
    level: str = "info"
    message: str = ""
    parsed: Optional[LogEventParsed] = None


class LogsResponse(BaseModel):
    events: List[LogEntry] = Field(default_factory=list)
    total: int = 0
    next_cursor: Optional[str] = None  # M4: cursor for next page


class ActionRequest(BaseModel):
    action: str
    target: str
    reason: str = ""
    duration_minutes: int = 60


class ActionResponse(BaseModel):
    status: str
    message: str = ""
    action_id: Optional[str] = None
    reversible: bool = False
    reversal_at: Optional[str] = None


class AlertInfo(BaseModel):
    id: Optional[str] = None  # M4: stream ID for cursor pagination
    alert_id: str = ""
    timestamp: str = ""
    policy_name: str = ""
    severity: str = "medium"
    score: float = 0.0
    risk_level: str = ""
    anomaly_type: str = ""
    source_ip: str = ""
    action: str = "alert_only"
    message: str = ""


class AlertsResponse(BaseModel):
    alerts: List[AlertInfo] = Field(default_factory=list)
    total: int = 0
    next_cursor: Optional[str] = None  # M4: cursor for next page


class ActionLogEntry(BaseModel):
    id: Optional[str] = None  # M4: stream ID for cursor pagination
    action_id: str = ""
    action: str = ""
    target: str = ""
    triggered_by: str = ""
    triggered_at: str = ""
    status: str = ""
    reversible: bool = False
    message: str = ""


class ActionsResponse(BaseModel):
    actions: List[ActionLogEntry] = Field(default_factory=list)
    total: int = 0
    next_cursor: Optional[str] = None  # M4: cursor for next page
