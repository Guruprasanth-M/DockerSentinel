"""Collector supervisor process."""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import sys
import time
from pathlib import Path
from typing import List

import structlog
import yaml
from redis.asyncio import Redis

from log_collector import discover_log_files, tail_log_file
from network_collector import run as run_network
from process_collector import run as run_process
from feature_builder import run as run_features
from state import CollectorState


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

log = structlog.get_logger(service="collectors")

# ─── Configuration ───
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
CONFIG_PATH = os.environ.get("HOSTSPECTRA_CONFIG", "/config/hostspectra.yml")
HEALTH_FILE = "/tmp/collectors_healthy"
HEARTBEAT_INTERVAL = 10  # seconds


def load_config() -> dict:
    """Load hostspectra configuration."""
    try:
        with open(CONFIG_PATH, "r") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        log.warning("config_not_found", path=CONFIG_PATH)
        return {}
    except Exception as e:
        log.error("config_load_error", error=str(e))
        return {}


async def supervisor(name: str, coro_factory, *args) -> None:
    """Supervisor loop — restarts a coroutine on crash with exponential backoff."""
    backoff = 2
    while True:
        try:
            log.info("supervisor_starting", component=name)
            await coro_factory(*args)
            backoff = 2  # Reset on clean exit
        except asyncio.CancelledError:
            log.info("supervisor_cancelled", component=name)
            raise
        except Exception as e:
            log.error("supervisor_crash", component=name, error=str(e), backoff=backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


async def main() -> None:
    """Main entry point — starts all collectors."""
    config = load_config()
    hostspectra_config = config.get("hostspectra", {})
    collection_config = hostspectra_config.get("collection", {})

    interval_ms = collection_config.get("interval_ms", 500)
    feature_window = collection_config.get("feature_window_seconds", 5)

    log.info("collectors_starting", redis_url=_mask_url(REDIS_URL))

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

    # Initialize state
    state = CollectorState()

    # Discover log files
    log_files = discover_log_files()

    # Build task list
    tasks: List[asyncio.Task] = []

    # Log collectors — one per log file
    for filepath in log_files:
        task = asyncio.create_task(
            supervisor(f"log:{os.path.basename(filepath)}", tail_log_file, filepath, redis, state)
        )
        tasks.append(task)

    # Network collector
    tasks.append(asyncio.create_task(
        supervisor("network", run_network, redis, interval_ms)
    ))

    # Process collector
    tasks.append(asyncio.create_task(
        supervisor("process", run_process, redis, interval_ms)
    ))

    # Feature builder
    tasks.append(asyncio.create_task(
        supervisor("features", run_features, redis, feature_window)
    ))

    # Heartbeat publisher
    async def heartbeat() -> None:
        while not shutdown_event.is_set():
            try:
                await redis.set(
                    "hostspectra:heartbeat:collectors",
                    json.dumps({
                        "status": "running",
                        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "pid": os.getpid(),
                        "task_count": len(tasks),
                    }),
                    ex=HEARTBEAT_INTERVAL * 2,
                )
            except Exception as e:
                log.error("heartbeat_failed", error=str(e))
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=HEARTBEAT_INTERVAL)
            except asyncio.TimeoutError:
                pass

    # Mark as healthy
    Path(HEALTH_FILE).touch()
    log.info("collectors_healthy", task_count=len(tasks))

    # Setup shutdown handler
    shutdown_event = asyncio.Event()

    def handle_signal(sig: int) -> None:
        log.info("shutdown_signal_received", signal=sig)
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, handle_signal, sig)

    # Wait for shutdown signal
    heartbeat_task = asyncio.create_task(heartbeat())
    await shutdown_event.wait()

    # Graceful shutdown
    log.info("graceful_shutdown_starting")

    # Cancel all tasks
    heartbeat_task.cancel()
    for task in tasks:
        task.cancel()

    await asyncio.gather(*tasks, return_exceptions=True)

    # Save state
    state.save()
    log.info("state_saved")

    # Remove health file
    try:
        os.remove(HEALTH_FILE)
    except OSError:
        pass

    # Close Redis
    await redis.aclose()
    log.info("collectors_shutdown_complete")


if __name__ == "__main__":
    asyncio.run(main())
