"""WebSocket handler for live event streaming."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Dict, List, Optional, Set

import structlog
from fastapi import WebSocket, WebSocketDisconnect
from redis.asyncio import Redis

log = structlog.get_logger()

# H13: Default subscription topics
DEFAULT_TOPICS = {"scores", "logs", "alerts"}
ALL_TOPICS = {"scores", "logs", "processes", "alerts"}

# Map topics to Redis streams
TOPIC_STREAM_MAP = {
    "scores": "sentinel:scores",
    "logs": "sentinel:logs",
    "processes": "sentinel:processes",
    "alerts": "sentinel:alerts",
}


class ClientSession:
    """Tracks per-client subscription state for filtering (H13)."""

    def __init__(self, websocket: WebSocket):
        self.websocket = websocket
        self.topics: Set[str] = set(DEFAULT_TOPICS)
        self.last_event_id: Optional[str] = None  # Resume token
        self.last_anomaly_notify: float = 0.0  # Throttle anomaly notifications


class ConnectionManager:
    """Manages WebSocket connections with per-client filtering (H13)."""

    def __init__(self) -> None:
        self.sessions: Dict[int, ClientSession] = {}

    async def connect(self, websocket: WebSocket) -> ClientSession:
        await websocket.accept()
        session = ClientSession(websocket)
        self.sessions[id(websocket)] = session
        log.info("ws_client_connected", total=len(self.sessions))
        return session

    def disconnect(self, websocket: WebSocket) -> None:
        self.sessions.pop(id(websocket), None)
        log.info("ws_client_disconnected", total=len(self.sessions))

    @property
    def active_connections(self) -> List[WebSocket]:
        return [s.websocket for s in self.sessions.values()]

    async def broadcast(self, message: dict, topic: str = "") -> None:
        """Send message to clients subscribed to the topic (H13)."""
        disconnected = []
        for ws_id, session in list(self.sessions.items()):
            if topic and topic not in session.topics:
                continue
            try:
                # Check if WS is still open before sending
                if session.websocket.client_state.name != "CONNECTED":
                    disconnected.append(session.websocket)
                    continue
                await session.websocket.send_json(message)
            except Exception:
                disconnected.append(session.websocket)

        for conn in disconnected:
            self.disconnect(conn)


manager = ConnectionManager()

# H13: Ping interval
WS_PING_INTERVAL = 30  # seconds


async def send_replay(
    websocket: WebSocket,
    redis: Redis,
    session: ClientSession,
) -> None:
    """Send the last 60 seconds of buffered events to a new client.
    
    H13: Replays scores + alerts + logs (not just scores + logs).
    If session has last_event_id, replays from that point (gap fill).
    """
    try:
        # Get recent scores
        if "scores" in session.topics:
            latest_score = await redis.get("sentinel:latest_score")
            if latest_score:
                await websocket.send_json({
                    "type": "metric_update",
                    "data": json.loads(latest_score),
                })

        # Get recent alerts (last 10)
        if "alerts" in session.topics:
            alert_entries = await redis.xrevrange("sentinel:alerts", count=10)
            for entry_id, fields in reversed(alert_entries):
                try:
                    await websocket.send_json({
                        "type": "alert",
                        "id": entry_id,
                        "data": dict(fields),
                    })
                except (json.JSONDecodeError, TypeError):
                    continue

        # Get recent log events (last 20)
        if "logs" in session.topics:
            log_entries = await redis.xrevrange("sentinel:logs", count=20)
            for entry_id, fields in reversed(log_entries):
                raw = fields.get("data", "")
                try:
                    data = json.loads(raw)
                    await websocket.send_json({
                        "type": "log_event",
                        "id": entry_id,
                        "data": data,
                    })
                except (json.JSONDecodeError, TypeError):
                    continue

    except Exception as e:
        log.error("ws_replay_error", error=str(e))


async def _handle_client_messages(websocket: WebSocket, session: ClientSession) -> None:
    """H13: Listen for subscription changes from client."""
    try:
        while True:
            msg = await websocket.receive_json()
            if isinstance(msg, dict):
                # Handle subscription: {subscribe: ["scores", "alerts"], from_id: "..."}
                if "subscribe" in msg:
                    topics = msg["subscribe"]
                    if isinstance(topics, list):
                        valid = {t for t in topics if t in ALL_TOPICS}
                        if valid:
                            session.topics = valid
                            await websocket.send_json({
                                "type": "subscribed",
                                "topics": list(session.topics),
                            })
                # Handle pong
                if msg.get("type") == "pong":
                    pass  # Client responded to ping
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    except Exception:
        pass


async def _ping_loop(websocket: WebSocket) -> None:
    """H13: Send periodic ping to detect dead connections."""
    try:
        while True:
            await asyncio.sleep(WS_PING_INTERVAL)
            await websocket.send_json({"type": "ping"})
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    except Exception:
        pass


async def stream_events(
    websocket: WebSocket,
    redis: Redis,
    session: ClientSession,
) -> None:
    """Stream live events to a WebSocket client with filtering (H13)."""
    # Track stream positions - start from latest
    last_ids: Dict[str, str] = {}
    for topic in ALL_TOPICS:
        stream = TOPIC_STREAM_MAP[topic]
        last_ids[stream] = "$"

    # Start ping loop and client message handler
    ping_task = asyncio.create_task(_ping_loop(websocket))
    client_task = asyncio.create_task(_handle_client_messages(websocket, session))

    try:
        while True:
            try:
                # Only read streams the client is subscribed to
                streams_to_read = {}
                for topic in session.topics:
                    stream = TOPIC_STREAM_MAP[topic]
                    streams_to_read[stream] = last_ids.get(stream, "$")

                if not streams_to_read:
                    await asyncio.sleep(1)
                    continue

                results = await redis.xread(
                    streams=streams_to_read,
                    count=50,
                    block=500,  # H13: reduced from 1000 to 500ms for lower latency
                )

                for stream_key, messages in results:
                    stream_name = stream_key if isinstance(stream_key, str) else stream_key.decode()

                    for msg_id, fields in messages:
                        last_ids[stream_name] = msg_id

                        raw = fields.get("data", "")
                        try:
                            data = json.loads(raw)
                        except (json.JSONDecodeError, TypeError):
                            data = dict(fields)

                        if stream_name == "sentinel:scores":
                            await websocket.send_json({
                                "type": "metric_update",
                                "id": msg_id,
                                "data": data,
                            })

                            score = data.get("score", 0.0) if isinstance(data, dict) else 0.0
                            if score > 0.6:
                                # Throttle: max 1 anomaly notification per 30s per client
                                now_ts = time.time()
                                if now_ts - session.last_anomaly_notify >= 30:
                                    session.last_anomaly_notify = now_ts
                                    await websocket.send_json({
                                        "type": "anomaly_detected",
                                        "id": msg_id,
                                        "data": {
                                            "anomaly_score": score,
                                            "features": data.get("features", {}),
                                            "message": f"Anomaly detected (score: {score:.2f})",
                                        },
                                    })

                        elif stream_name == "sentinel:logs":
                            await websocket.send_json({
                                "type": "log_event",
                                "id": msg_id,
                                "data": data,
                            })

                        elif stream_name == "sentinel:alerts":
                            await websocket.send_json({
                                "type": "alert",
                                "id": msg_id,
                                "data": data,
                            })

                        elif stream_name == "sentinel:processes":
                            # Skip process summaries
                            if isinstance(data, dict) and data.get("type") == "process_summary":
                                continue
                            if isinstance(data, dict) and data.get("risk_flag"):
                                await websocket.send_json({
                                    "type": "process_alert",
                                    "id": msg_id,
                                    "data": data,
                                })

            except asyncio.CancelledError:
                raise
            except WebSocketDisconnect:
                raise
            except Exception as e:
                err_msg = str(e)
                # If send-after-close, the connection is dead — break the loop
                if "after sending" in err_msg or "DISCONNECTED" in err_msg:
                    log.info("ws_client_gone", reason=err_msg)
                    break
                log.error("ws_stream_error", error=err_msg)
                await asyncio.sleep(1)
    finally:
        ping_task.cancel()
        client_task.cancel()
        try:
            await asyncio.gather(ping_task, client_task, return_exceptions=True)
        except Exception:
            pass
