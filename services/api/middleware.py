"""Auth and rate-limiting middleware."""

from __future__ import annotations

import os
import time
from typing import Callable, Optional

import structlog
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

log = structlog.get_logger()

RATE_LIMIT = 600  # requests per minute (external clients)
RATE_WINDOW = 60  # seconds
MAX_PAYLOAD_BYTES = 1_048_576  # 1 MiB max request body

# Docker internal network prefixes — exempt from rate limiting
_INTERNAL_PREFIXES = ("172.", "10.", "192.168.", "127.")

# ─── H1: Cached token with TTL ───────────────────────────
_cached_token: Optional[str] = None
_token_cached_at: float = 0.0
_TOKEN_CACHE_TTL = 60.0  # seconds


def get_api_token() -> str:
    """Get the API token, cached in memory with 60s TTL (H1)."""
    global _cached_token, _token_cached_at
    import yaml

    now = time.time()
    if _cached_token is not None and (now - _token_cached_at) < _TOKEN_CACHE_TTL:
        return _cached_token

    # Try config file first
    config_path = os.environ.get("SENTINEL_CONFIG", "/config/sentinel.yml")
    try:
        with open(config_path, "r") as f:
            config = yaml.safe_load(f) or {}
        token = config.get("sentinel", {}).get("api_token", "")
    except Exception:
        token = ""

    # Fall back to environment variable
    if not token:
        token = os.environ.get("SENTINEL_API_TOKEN", "")

    _cached_token = token
    _token_cached_at = now
    return token


# ─── H2: Redis-backed rate limiter ───────────────────────
async def check_rate_limit_redis(client_ip: str, redis_client) -> bool:
    """Check rate limit using Redis sliding window (H2).

    Uses Redis INCR + EXPIRE for distributed, restart-persistent rate limiting.
    Returns True if allowed, False if rate limited.
    """
    if redis_client is None:
        return True  # Fail open if Redis unavailable

    key = f"sentinel:ratelimit:{client_ip}"
    try:
        current = await redis_client.incr(key)
        if current == 1:
            await redis_client.expire(key, RATE_WINDOW)
        return current <= RATE_LIMIT
    except Exception:
        return True  # Fail open on Redis errors


class AuthMiddleware(BaseHTTPMiddleware):
    """Authentication and rate limiting middleware."""

    # Paths that don't require authentication
    PUBLIC_PATHS = {"/health", "/metrics", "/status", "/system-info", "/containers", "/dashboard-data", "/dashboard-fast", "/docs", "/openapi.json"}

    # Write endpoints that always require a valid token
    PROTECTED_METHODS = {"POST", "PUT", "DELETE", "PATCH"}

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        path = request.url.path
        method = request.method

        # Get client IP — trust X-Real-IP/X-Forwarded-For only from Docker proxy
        peer_ip = request.client.host if request.client else "unknown"
        if peer_ip.startswith(_INTERNAL_PREFIXES):
            # Request came from Docker network (nginx proxy) — use forwarded header
            client_ip = (
                request.headers.get("X-Real-IP")
                or (request.headers.get("X-Forwarded-For", "").split(",")[0].strip())
                or peer_ip
            )
        else:
            # Direct connection — use peer address
            client_ip = peer_ip

        # H2: Redis-backed rate limiting (skip internal Docker network)
        redis_client = getattr(request.app.state, "redis", None)
        if not client_ip.startswith(_INTERNAL_PREFIXES):
            if not await check_rate_limit_redis(client_ip, redis_client):
                log.warning("rate_limit_exceeded", client_ip=client_ip, path=path)
                return JSONResponse(
                    status_code=429, content={"detail": "Rate limit exceeded"}
                )

        # H3: Payload size limit — reject oversized request bodies
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > MAX_PAYLOAD_BYTES:
            return JSONResponse(
                status_code=413,
                content={"detail": f"Request body too large. Max {MAX_PAYLOAD_BYTES} bytes."},
            )

        # TODO: Replace with role-based auth when user management is implemented
        # (admin = read/write all, user = read-only, no actions)
        # Skip auth for public paths and WebSocket
        if path in self.PUBLIC_PATHS or path.startswith("/ws/"):
            return await call_next(request)

        # H1: Check API token (cached)
        api_token = get_api_token()
        if api_token and api_token != "CHANGE_THIS_TO_64_CHAR_HEX_STRING":
            # Extract token from either header
            request_token = (
                request.headers.get("X-Sentinel-Token", "")
                or self._extract_bearer(request)
            )

            if request_token and request_token == api_token:
                # Valid token provided, allow any method
                return await call_next(request)

            # No valid token: block write operations, allow reads
            if method in self.PROTECTED_METHODS:
                if not request_token:
                    return JSONResponse(
                        status_code=401,
                        content={"detail": "Authentication required. Provide X-Sentinel-Token or Authorization: Bearer <token>"},
                    )
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Invalid API token"},
                )

        return await call_next(request)

    @staticmethod
    def _extract_bearer(request: Request) -> str:
        """Extract token from Authorization: Bearer <token> header."""
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:]
        return ""
