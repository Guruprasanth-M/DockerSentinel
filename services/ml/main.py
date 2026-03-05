"""ML scoring loop."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path

import structlog
import yaml
from redis.asyncio import Redis

from scorer import Scorer

import re


def _mask_url(url: str) -> str:
    """Mask passwords in connection URLs for safe logging."""
    return re.sub(r'(://[^:]*:)[^@]+(@)', r'\1*****\2', url)


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

log = structlog.get_logger(service="ml")

# ─── Configuration ───
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
CONFIG_PATH = os.environ.get("SENTINEL_CONFIG", "/config/sentinel.yml")
HEALTH_FILE = "/tmp/ml_healthy"
HEARTBEAT_INTERVAL = 10  # seconds


def load_config() -> dict:
    """Load sentinel configuration."""
    try:
        with open(CONFIG_PATH, "r") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        log.warning("config_not_found", path=CONFIG_PATH)
        return {}
    except Exception as e:
        log.error("config_load_error", error=str(e))
        return {}


async def process_features(redis: Redis, scorer: Scorer) -> None:
    """Read feature vectors from Redis and score them using consumer group."""
    stream_name = "sentinel:features"
    output_stream = "sentinel:scores"
    consumer_group = "ml_scoring"
    consumer_name = f"ml_{os.getpid()}"
    maxlen = 10000

    # Ensure consumer group exists (C6/H3: use xreadgroup instead of xread)
    try:
        await redis.xgroup_create(stream_name, consumer_group, id="0", mkstream=True)
        log.info("ml_consumer_group_created", group=consumer_group)
    except Exception as e:
        if "BUSYGROUP" not in str(e):
            raise
        log.info("ml_consumer_group_exists", group=consumer_group)

    log.info("ml_scoring_loop_start", consumer_group=consumer_group, consumer=consumer_name)

    # First process any pending messages (from previous crashed runs)
    processed_pending = False
    last_id = "0"

    while True:
        try:
            if not processed_pending:
                # Read pending messages first
                results = await redis.xreadgroup(
                    consumer_group, consumer_name,
                    {stream_name: last_id},
                    count=10, block=1000,
                )
                has_msgs = results and any(msgs for _, msgs in results)
                if not has_msgs:
                    processed_pending = True
                    continue
            else:
                # Read new messages (H11: reduced block from 5000 to 2000ms)
                results = await redis.xreadgroup(
                    consumer_group, consumer_name,
                    {stream_name: ">"},
                    count=10, block=2000,
                )

            if not results:
                continue

            for stream_key, messages in results:
                for msg_id, fields in messages:
                    raw = fields.get("data") or fields.get(b"data", b"")
                    if isinstance(raw, bytes):
                        raw = raw.decode()

                    try:
                        features = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        log.warning("ml_dead_letter", msg_id=msg_id, reason="json_decode_error")
                        try:
                            await redis.xadd(
                                "sentinel:dead_letter",
                                {"source": "ml", "msg_id": msg_id, "reason": "json_decode_error",
                                 "data": raw[:500] if raw else "",
                                 "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
                                maxlen=1000, approximate=True,
                            )
                        except Exception:
                            pass
                        await redis.xack(stream_name, consumer_group, msg_id)
                        continue

                    # Score the feature vector
                    result = scorer.score(features)

                    # Add timestamp from original features
                    result["timestamp"] = features.get("timestamp", "")
                    result["features"] = features

                    # Publish score to Redis
                    await redis.xadd(
                        output_stream,
                        {"data": json.dumps(result)},
                        maxlen=maxlen,
                        approximate=True,
                    )

                    # Store latest score for quick access
                    await redis.set("sentinel:latest_score", json.dumps(result))

                    # Acknowledge processed message
                    await redis.xack(stream_name, consumer_group, msg_id)

                    if result["score"] > 0.6:
                        log.warning(
                            "anomaly_detected",
                            score=result["score"],
                            risk_level=result["risk_level"],
                        )
                        # Increment 24h anomaly counter with auto-expiry
                        try:
                            count = await redis.incr("sentinel:anomaly_count_24h")
                            if count == 1:
                                await redis.expire("sentinel:anomaly_count_24h", 86400)
                        except Exception:
                            pass

        except asyncio.CancelledError:
            raise
        except Exception as e:
            _backoff = min(getattr(process_features, '_backoff', 1) * 2, 60)
            process_features._backoff = _backoff
            log.error("scoring_error", error=str(e), backoff=_backoff)
            await asyncio.sleep(_backoff)


async def main() -> None:
    """Main entry point."""
    config = load_config()

    log.info("ml_engine_starting", redis_url=_mask_url(REDIS_URL))

    # Connect to Redis with retry
    redis = None
    for attempt in range(30):
        try:
            redis = Redis.from_url(REDIS_URL, decode_responses=True)
            await redis.ping()
            log.info("redis_connected")
            break
        except Exception as e:
            delay = min(2 ** attempt, 60)
            log.warning("redis_connection_retry", attempt=attempt + 1, delay=delay, error=str(e))
            await asyncio.sleep(delay)
    else:
        log.error("redis_connection_failed", attempts=30)
        sys.exit(1)

    # Load scorer
    scorer = Scorer(model_dir="/data/models")

    if not scorer.is_ready:
        log.warning("ml_model_not_ready", msg="Running without ML model — scores will be 0.0")

    # Mark as healthy
    Path(HEALTH_FILE).touch()
    log.info("ml_engine_healthy", model_ready=scorer.is_ready)

    # Setup shutdown handler
    shutdown_event = asyncio.Event()

    def handle_signal(sig: int) -> None:
        log.info("shutdown_signal_received", signal=sig)
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, handle_signal, sig)

    # Start scoring task
    scoring_task = asyncio.create_task(process_features(redis, scorer))

    # Heartbeat publisher
    async def heartbeat() -> None:
        while not shutdown_event.is_set():
            try:
                await redis.set(
                    "sentinel:heartbeat:ml",
                    json.dumps({
                        "status": "active",
                        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "pid": os.getpid(),
                        "model_ready": scorer.is_ready,
                    }),
                    ex=HEARTBEAT_INTERVAL * 2,
                )
            except Exception as e:
                log.error("heartbeat_failed", error=str(e))
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=HEARTBEAT_INTERVAL)
            except asyncio.TimeoutError:
                pass

    heartbeat_task = asyncio.create_task(heartbeat())

    # Wait for shutdown
    await shutdown_event.wait()

    # Graceful shutdown
    log.info("ml_shutdown_starting")
    heartbeat_task.cancel()
    scoring_task.cancel()
    try:
        await scoring_task
    except asyncio.CancelledError:
        pass

    try:
        os.remove(HEALTH_FILE)
    except OSError:
        pass

    await redis.aclose()
    log.info("ml_shutdown_complete")


if __name__ == "__main__":
    asyncio.run(main())
