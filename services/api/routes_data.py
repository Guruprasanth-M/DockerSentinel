"""Log, alert, action, and config endpoints."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import List, Optional

import structlog
from fastapi import APIRouter, HTTPException, Query, Request
from redis.asyncio import Redis

from schemas import (
    ActionLogEntry,
    ActionRequest,
    ActionResponse,
    ActionsResponse,
    AlertInfo,
    AlertsResponse,
    LogEntry,
    LogEventParsed,
    LogsResponse,
    ScoreEntry,
    ScoresResponse,
)

log = structlog.get_logger()

router = APIRouter()


def get_redis(request) -> Redis:
    """Get Redis client from app state."""
    return request.app.state.redis


@router.get("/logs", response_model=LogsResponse)
async def logs(
    request: Request,
    limit: int = Query(default=100, ge=1, le=1000),
    level: Optional[str] = Query(default=None, pattern="^(info|warning|error|critical)$"),
    source: Optional[str] = Query(default=None),
    after: Optional[str] = Query(default=None, description="M4: Cursor - stream ID to start after"),
):
    """Recent log events from the log stream. Supports cursor-based pagination (M4)."""
    redis: Redis = get_redis(request)

    events: List[LogEntry] = []
    next_cursor: Optional[str] = None
    has_filter = bool(level or source)
    # When filters are active, scan more entries per batch to fill the
    # requested limit (auth.log can dominate the stream 70%+).
    batch_size = limit * 10 if has_filter else limit * 2
    max_scanned = 10000  # Safety cap to avoid scanning the entire stream

    try:
        cursor_id = after if after else "+"
        scanned = 0

        while len(events) < limit and scanned < max_scanned:
            if cursor_id == "+":
                entries = await redis.xrevrange("hostspectra:logs", count=batch_size)
            else:
                entries = await redis.xrevrange(
                    "hostspectra:logs", max=cursor_id, count=batch_size
                )
                # Skip the cursor entry itself (xrevrange max is inclusive)
                if entries and entries[0][0] == cursor_id:
                    entries = entries[1:]

            if not entries:
                break

            scanned += len(entries)

            for entry_id, fields in entries:
                raw = fields.get("data", "")
                try:
                    data = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue

                event_level = data.get("level", "info")
                event_source = data.get("source", "")

                # Apply filters
                if level and event_level != level:
                    continue
                if source and source not in event_source:
                    continue

                parsed = None
                if data.get("type") and data["type"] != "unknown":
                    parsed = LogEventParsed(
                        type=data.get("type", "unknown"),
                        user=data.get("user"),
                        source_ip=data.get("source_ip"),
                    )

                events.append(LogEntry(
                    id=entry_id,
                    timestamp=data.get("timestamp", ""),
                    source=event_source,
                    level=event_level,
                    message=data.get("message", ""),
                    parsed=parsed,
                ))

                if len(events) >= limit:
                    next_cursor = entry_id
                    break

            if len(events) >= limit:
                break

            # Move cursor to continue scanning
            cursor_id = entries[-1][0]

            # If no filter, one pass is enough
            if not has_filter:
                break

    except Exception as e:
        log.error("logs_query_error", error=str(e))

    return LogsResponse(events=events, total=len(events), next_cursor=next_cursor)


@router.get("/logs/sources")
async def log_sources(request: Request):
    """Return distinct log sources currently in the stream."""
    redis: Redis = get_redis(request)
    sources: set = set()
    try:
        entries = await redis.xrevrange("hostspectra:logs", count=2000)
        for _, fields in entries:
            raw = fields.get("data", "")
            try:
                data = json.loads(raw)
                src = data.get("source", "")
                if src:
                    sources.add(src)
            except (json.JSONDecodeError, TypeError):
                continue
    except Exception as e:
        log.error("log_sources_error", error=str(e))
    return {"sources": sorted(sources)}


@router.get("/config")
async def get_config(request: Request):
    """Get current configuration (redacted for security)."""
    import yaml

    config_path = request.app.state.config_path
    try:
        with open(config_path, "r") as f:
            config = yaml.safe_load(f) or {}

        # Redact sensitive fields
        hostspectra_cfg = config.get("hostspectra", {})
        if "api_token" in hostspectra:
            hostspectra_cfg["api_token"] = "***REDACTED***"

        # Redact stream limits and ML thresholds from attackers
        safe_config = {
            "version": config.get("version", ""),
            "hostspectra": {
                "host_name": hostspectra_cfg.get("host_name", ""),
                "collection": {
                    "interval_ms": hostspectra_cfg.get("collection", {}).get("interval_ms", 5000),
                },
                "logging": hostspectra_cfg.get("logging", {}),
            },
        }
        return safe_config
    except Exception as e:
        return {"error": str(e)}


@router.post("/action", response_model=ActionResponse)
async def execute_action(request: Request, action: ActionRequest):
    """Execute a manual action (block IP, kill process, etc.). M5: returns action_id for tracking."""
    redis: Redis = get_redis(request)

    action_id = f"manual_{int(time.time())}_{action.target}"
    timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    try:
        await redis.xadd(
            "hostspectra:action_requests",
            {
                "alert_id": "",
                "action_id": action_id,
                "action": action.action,
                "target": action.target,
                "triggered_by": "manual",
                "reason": action.reason,
                "duration_minutes": str(action.duration_minutes),
                "policy_name": "manual",
                "severity": "high",
                "timestamp": timestamp,
            },
            maxlen=5000,
            approximate=True,
        )

        return ActionResponse(
            status="queued",
            message=f"Action '{action.action}' on '{action.target}' queued for execution. Track via GET /actions?action_id={action_id}",
            action_id=action_id,
            reversible=action.action == "block_ip",
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to queue action: {str(e)}",
        )


@router.get("/alerts", response_model=AlertsResponse)
async def alerts(
    request: Request,
    limit: int = Query(default=50, ge=1, le=500),
    severity: Optional[str] = Query(default=None, pattern="^(low|medium|high|critical)$"),
    since: Optional[str] = Query(default=None),
    after: Optional[str] = Query(default=None, description="M4: Cursor - stream ID to start after"),
):
    """Recent security alerts. M4: cursor-based pagination via ?after=stream_id."""
    redis: Redis = get_redis(request)

    alert_list: List[AlertInfo] = []
    next_cursor: Optional[str] = None

    try:
        if after:
            entries = await redis.xrevrange("hostspectra:alerts", max=after, count=limit * 2)
            if entries and entries[0][0] == after:
                entries = entries[1:]
        else:
            entries = await redis.xrevrange("hostspectra:alerts", count=limit * 2)

        for entry_id, fields in entries:
            try:
                alert_severity = fields.get("severity", "medium")
                if severity and alert_severity != severity:
                    continue

                alert_timestamp = fields.get("timestamp", "")
                if since:
                    try:
                        since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
                        alert_dt = datetime.fromisoformat(alert_timestamp.replace("Z", "+00:00"))
                        if alert_dt < since_dt:
                            continue
                    except (ValueError, TypeError):
                        pass

                # Parse details if JSON
                details = fields.get("details", "{}")
                try:
                    details = json.loads(details) if isinstance(details, str) else details
                except (json.JSONDecodeError, TypeError):
                    details = {}

                alert_list.append(AlertInfo(
                    id=entry_id,
                    alert_id=fields.get("alert_id", entry_id),
                    timestamp=alert_timestamp,
                    policy_name=fields.get("policy_name", ""),
                    severity=alert_severity,
                    score=float(fields.get("score", 0.0)),
                    risk_level=fields.get("risk_level", ""),
                    anomaly_type=fields.get("anomaly_type", ""),
                    source_ip=fields.get("source_ip", ""),
                    action=fields.get("action", "alert_only"),
                    message=fields.get("message", ""),
                ))

                if len(alert_list) >= limit:
                    next_cursor = entry_id
                    break

            except Exception as e:
                log.error("alert_parse_error", error=str(e))
                continue

    except Exception as e:
        log.error("alerts_query_error", error=str(e))

    total_count = 0
    try:
        total_count_str = await redis.get("hostspectra:alert_count")
        total_count = int(total_count_str) if total_count_str else len(alert_list)
    except Exception:
        total_count = len(alert_list)

    return AlertsResponse(alerts=alert_list, total=total_count, next_cursor=next_cursor)


@router.get("/actions", response_model=ActionsResponse)
async def actions(
    request: Request,
    limit: int = Query(default=50, ge=1, le=500),
    since: Optional[str] = Query(default=None),
    triggered_by: Optional[str] = Query(default=None, pattern="^(policy|manual)$"),
    action_id: Optional[str] = Query(default=None, description="M5: Filter by action_id for tracking"),
    after: Optional[str] = Query(default=None, description="M4: Cursor - stream ID to start after"),
):
    """Recent action history. M4: cursor pagination. M5: filter by action_id."""
    redis: Redis = get_redis(request)

    action_list: List[ActionLogEntry] = []
    next_cursor: Optional[str] = None

    try:
        if after:
            entries = await redis.xrevrange("hostspectra:actions", max=after, count=limit * 2)
            if entries and entries[0][0] == after:
                entries = entries[1:]
        else:
            entries = await redis.xrevrange("hostspectra:actions", count=limit * 2)

        for entry_id, fields in entries:
            try:
                entry_triggered = fields.get("triggered_by", "")
                if triggered_by and entry_triggered != triggered_by:
                    continue

                # M5: Filter by action_id for tracking
                entry_action_id = fields.get("action_id", "")
                if action_id and entry_action_id != action_id:
                    continue

                entry_ts = fields.get("timestamp", "")
                if since:
                    try:
                        since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
                        entry_dt = datetime.fromisoformat(entry_ts.replace("Z", "+00:00"))
                        if entry_dt < since_dt:
                            continue
                    except (ValueError, TypeError):
                        pass

                action_list.append(ActionLogEntry(
                    id=entry_id,
                    action_id=fields.get("action_id", entry_id),
                    action=fields.get("action", ""),
                    target=fields.get("target", ""),
                    triggered_by=entry_triggered,
                    triggered_at=entry_ts,
                    status=fields.get("status", ""),
                    reversible=fields.get("reversible", "false").lower() == "true",
                    message=fields.get("message", ""),
                ))

                if len(action_list) >= limit:
                    next_cursor = entry_id
                    break

            except Exception as e:
                log.error("action_parse_error", error=str(e))
                continue

    except Exception as e:
        log.error("actions_query_error", error=str(e))

    return ActionsResponse(actions=action_list, total=len(action_list), next_cursor=next_cursor)


@router.get("/scores", response_model=ScoresResponse)
async def scores(
    request: Request,
    limit: int = Query(default=50, ge=1, le=500),
    after: Optional[str] = Query(default=None, description="Cursor - stream ID to start after"),
):
    """Recent ML anomaly scores from hostspectra:scores stream."""
    redis: Redis = get_redis(request)

    score_list: List[ScoreEntry] = []
    next_cursor: Optional[str] = None

    try:
        if after:
            entries = await redis.xrevrange("hostspectra:scores", max=after, count=limit + 1)
            if entries and entries[0][0] == after:
                entries = entries[1:]
        else:
            entries = await redis.xrevrange("hostspectra:scores", count=limit + 1)

        for entry_id, fields in entries:
            try:
                raw = fields.get("data", "")
                if raw:
                    data = json.loads(raw) if isinstance(raw, str) else raw
                else:
                    data = fields

                features = data.get("features", {})
                if isinstance(features, str):
                    try:
                        features = json.loads(features)
                    except (json.JSONDecodeError, TypeError):
                        features = {}

                score_list.append(ScoreEntry(
                    id=entry_id,
                    timestamp=data.get("timestamp", ""),
                    score=float(data.get("score", 0.0)),
                    risk_level=data.get("risk_level", "normal"),
                    isolation_forest_score=float(data.get("isolation_forest_score", 0.0)),
                    zscore_score=float(data.get("zscore_score", 0.0)),
                    ema_score=float(data.get("ema_score", 0.0)),
                    model_version=data.get("model_version", ""),
                    features=features,
                ))

                if len(score_list) >= limit:
                    next_cursor = entry_id
                    break

            except Exception as e:
                log.error("score_parse_error", error=str(e))
                continue

    except Exception as e:
        log.error("scores_query_error", error=str(e))

    return ScoresResponse(scores=score_list, total=len(score_list), next_cursor=next_cursor)
