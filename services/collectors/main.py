"""Collector supervisor process."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
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
CONFIG_PATH = os.environ.get("SENTINEL_CONFIG", "/config/sentinel.yml")
HEALTH_FILE = "/tmp/collectors_healthy"


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


async def supervisor(name: str, coro_factory, *args) -> None:
    """Supervisor loop — restarts a coroutine on crash."""
    while True:
        try:
            log.info("supervisor_starting", component=name)
            await coro_factory(*args)
        except asyncio.CancelledError:
            log.info("supervisor_cancelled", component=name)
            raise
        except Exception as e:
            log.error("supervisor_crash", component=name, error=str(e))
            await asyncio.sleep(2)


async def main() -> None:
    """Main entry point — starts all collectors."""
    config = load_config()
    sentinel_config = config.get("sentinel", {})
    collection_config = sentinel_config.get("collection", {})

    interval_ms = collection_config.get("interval_ms", 500)
    feature_window = collection_config.get("feature_window_seconds", 5)

    log.info("collectors_starting", redis_url=REDIS_URL)

    # Connect to Redis
    redis = Redis.from_url(REDIS_URL, decode_responses=True)
    try:
        await redis.ping()
        log.info("redis_connected")
    except Exception as e:
        log.error("redis_connection_failed", error=str(e))
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
    await shutdown_event.wait()

    # Graceful shutdown
    log.info("graceful_shutdown_starting")

    # Cancel all tasks
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
