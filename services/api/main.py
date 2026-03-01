"""FastAPI server entry point."""

from __future__ import annotations

import asyncio
import os
import signal
import time
from contextlib import asynccontextmanager

import structlog
import yaml
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from redis.asyncio import Redis

from middleware import AuthMiddleware, get_api_token
from routes import router
from websocket_handler import manager, send_replay, stream_events

# ─── Structured logging setup ───
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.BoundLogger,
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
)

log = structlog.get_logger(service="api")

# ─── Configuration ───
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
CONFIG_PATH = os.environ.get("SENTINEL_CONFIG", "/config/sentinel.yml")

# M9: Disable /docs in production unless explicitly enabled
_ENABLE_DOCS = os.environ.get("SENTINEL_ENABLE_DOCS", "false").lower() in ("true", "1", "yes")

# ─── Lifespan ───
@asynccontextmanager
async def lifespan(application: FastAPI):
    """Manage startup and shutdown of the application."""
    log.info("api_starting", redis_url=REDIS_URL)

    redis = Redis.from_url(REDIS_URL, decode_responses=True)
    try:
        await redis.ping()
        log.info("redis_connected")
    except Exception as e:
        log.error("redis_connection_failed", error=str(e))

    application.state.redis = redis
    application.state.config_path = CONFIG_PATH
    application.state.start_time = time.time()

    heartbeat_task = asyncio.create_task(_heartbeat_loop(redis))
    status_task = asyncio.create_task(_status_broadcast_loop(application))
    log.info("api_ready")

    yield

    log.info("api_shutting_down")
    heartbeat_task.cancel()
    status_task.cancel()
    if hasattr(application.state, "redis"):
        await application.state.redis.aclose()
    log.info("api_shutdown_complete")


# ─── FastAPI app ───
app = FastAPI(
    title="Docker Sentinel API",
    version="v0.1",
    docs_url="/docs" if _ENABLE_DOCS else None,  # M9: disabled by default
    redoc_url=None,
    openapi_url="/openapi.json" if _ENABLE_DOCS else None,  # P1: hide schema in prod
    lifespan=lifespan,
)

# M8: Add CORS middleware — restrict origins in production
_cors_env = os.environ.get("CORS_ORIGINS", "")
_cors_origins = [o.strip() for o in _cors_env.split(",") if o.strip()] if _cors_env else []
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins or ["http://localhost:3000", "http://localhost:8080"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    allow_headers=["X-Sentinel-Token", "Authorization", "Content-Type"],
    expose_headers=["X-Sentinel-Token"],
)

# Add auth middleware
app.add_middleware(AuthMiddleware)


# P2: Sanitize validation errors — strip raw user input to prevent XSS reflection
@app.exception_handler(RequestValidationError)
async def _validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    safe_errors = []
    for err in exc.errors():
        sanitised = {k: v for k, v in err.items() if k != "input"}
        safe_errors.append(sanitised)
    return JSONResponse(status_code=422, content={"detail": safe_errors})


# Include routes
app.include_router(router)


async def _heartbeat_loop(redis: Redis) -> None:
    """Publish API heartbeat to Redis."""
    while True:
        try:
            await redis.set("sentinel:heartbeat:api", str(time.time()), ex=60)
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            break
        except Exception:
            await asyncio.sleep(5)


async def _status_broadcast_loop(application) -> None:
    """Push health+metrics to all WS clients every 2s for synchronized updates."""
    from routes_health import health as health_handler, metrics as metrics_handler

    class _FakeRequest:
        def __init__(self, app):
            self.app = app
    
    await asyncio.sleep(5)  # Wait for startup

    while True:
        try:
            if not manager.sessions:
                await asyncio.sleep(2)
                continue

            fake_req = _FakeRequest(application)
            
            health_data = await health_handler(fake_req)
            metrics_data = await metrics_handler(fake_req)

            payload = {
                "type": "status_update",
                "data": {
                    "health": health_data.model_dump() if hasattr(health_data, 'model_dump') else health_data.dict(),
                    "metrics": metrics_data.model_dump() if hasattr(metrics_data, 'model_dump') else metrics_data.dict(),
                },
            }

            await manager.broadcast(payload)

            await asyncio.sleep(2)
        except asyncio.CancelledError:
            break
        except Exception:
            await asyncio.sleep(5)


@app.websocket("/ws/live")
async def websocket_live(websocket: WebSocket):
    """Live event WebSocket endpoint."""
    # TODO: Implement proper auth when user management is added (admin = read/write, user = read-only)
    # Currently allows unauthenticated WebSocket for dashboard. Reject only wrong tokens.
    token = websocket.query_params.get("token", "")
    api_token = get_api_token()
    if api_token and api_token != "CHANGE_THIS_TO_64_CHAR_HEX_STRING":
        if token and token != api_token:
            await websocket.close(code=4003, reason="Invalid token")
            return

    session = await manager.connect(websocket)

    try:
        redis = app.state.redis

        # Send replay of recent events (H13: session-aware)
        await send_replay(websocket, redis, session)

        # Stream live events (H13: filtered by subscription)
        await stream_events(websocket, redis, session)

    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.error("ws_error", error=str(e))
    finally:
        manager.disconnect(websocket)
