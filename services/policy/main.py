"""Policy engine service."""
import asyncio
import json
import os
import signal
import time
from pathlib import Path

import asyncpg
import redis.asyncio as aioredis
import structlog

from engine import PolicyEngine
from loader import PolicyLoader

import re


def _mask_url(url: str) -> str:
    """Mask passwords in connection URLs for safe logging."""
    return re.sub(r'(://[^:]*:)[^@]+(@)', r'\1*****\2', url)


structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer(),
    ]
)

logger = structlog.get_logger("sentinel.policy")

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
DB_URL = os.environ.get("DB_URL", "")
HEARTBEAT_INTERVAL = 30
CONSUMER_GROUP = "policy_engine"
CONSUMER_NAME = f"policy_{os.getpid()}"
HEALTH_FILE = "/tmp/policy_healthy"

shutdown_event = asyncio.Event()


def handle_signal(sig: int) -> None:
    logger.info("policy_signal_received", signal=sig)
    shutdown_event.set()


def mark_healthy():
    Path(HEALTH_FILE).touch()


def mark_unhealthy():
    try:
        Path(HEALTH_FILE).unlink(missing_ok=True)
    except Exception:
        pass


async def connect_redis() -> aioredis.Redis:
    max_retries = 30
    for attempt in range(1, max_retries + 1):
        try:
            client = aioredis.from_url(REDIS_URL, decode_responses=True)
            await client.ping()
            logger.info("policy_redis_connected", attempt=attempt)
            return client
        except Exception as e:
            wait = min(2 ** attempt, 60)
            logger.warning("policy_redis_retry", attempt=attempt, wait=wait, error=str(e))
            await asyncio.sleep(wait)
    raise ConnectionError("Failed to connect to Redis after all retries")


async def connect_db() -> asyncpg.Pool | None:
    """Connect to PostgreSQL with retry."""
    if not DB_URL:
        logger.warning("policy_db_url_not_set")
        return None

    max_retries = 15
    for attempt in range(1, max_retries + 1):
        try:
            pool = await asyncpg.create_pool(DB_URL, min_size=1, max_size=3, command_timeout=10)
            async with pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            logger.info("policy_db_connected", attempt=attempt)
            return pool
        except Exception as e:
            wait = min(2 ** attempt, 30)
            logger.warning("policy_db_retry", attempt=attempt, wait=wait, error=str(e))
            await asyncio.sleep(wait)

    logger.error("policy_db_connection_failed")
    return None


INSERT_ALERT_SQL = """
    INSERT INTO alerts (alert_id, severity, score, risk_level, anomaly_type,
                        policy_name, source_ip, action, message, notify)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
    ON CONFLICT (alert_id) DO NOTHING
"""


async def persist_alert_to_db(db_pool, alert: dict, redis_client) -> None:
    """Insert alert into PostgreSQL. Dead-letters to Redis on failure."""
    if db_pool is None:
        return
    try:
        async with db_pool.acquire() as conn:
            await conn.execute(
                INSERT_ALERT_SQL,
                alert.get("alert_id", ""),
                alert.get("severity", "medium"),
                float(alert.get("score", 0.0)),
                alert.get("risk_level", "normal"),
                alert.get("anomaly_type", ""),
                alert.get("policy_name", ""),
                alert.get("source_ip", ""),
                alert.get("action", "alert_only"),
                alert.get("message", ""),
                alert.get("notify", False),
            )
    except Exception as e:
        logger.error("alert_db_persist_failed", alert_id=alert.get("alert_id"), error=str(e))
        try:
            await redis_client.rpush("sentinel:alerts:failed", json.dumps(alert))
        except Exception:
            pass


async def ensure_consumer_group(client: aioredis.Redis, stream: str, group: str):
    try:
        await client.xgroup_create(stream, group, id="0", mkstream=True)
        logger.info("consumer_group_created", stream=stream, group=group)
    except aioredis.ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise


async def heartbeat(client: aioredis.Redis) -> None:
    while not shutdown_event.is_set():
        try:
            await client.set(
                "sentinel:heartbeat:policy_engine",
                json.dumps({
                    "status": "active",
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "pid": os.getpid(),
                }),
                ex=HEARTBEAT_INTERVAL * 2,
            )
            mark_healthy()
        except Exception as e:
            logger.error("heartbeat_failed", error=str(e))
            mark_unhealthy()
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=HEARTBEAT_INTERVAL)
        except asyncio.TimeoutError:
            pass


async def process_scores(client: aioredis.Redis, engine: PolicyEngine, loader: PolicyLoader, db_pool: asyncpg.Pool | None):
    stream = "sentinel:scores"
    alert_stream = "sentinel:alerts"
    audit_stream = "sentinel:audit"

    await ensure_consumer_group(client, stream, CONSUMER_GROUP)
    await ensure_consumer_group(client, alert_stream, "alert_consumers")

    logger.info("policy_engine_processing", stream=stream, consumer=CONSUMER_NAME)

    last_id = "0"
    processed_pending = False

    while not shutdown_event.is_set():
        try:
            if not processed_pending:
                results = await client.xreadgroup(
                    CONSUMER_GROUP, CONSUMER_NAME,
                    {stream: last_id}, count=10, block=1000,
                )
                has_msgs = results and any(msgs for _, msgs in results)
                if not has_msgs:
                    processed_pending = True
                    continue
            else:
                results = await client.xreadgroup(
                    CONSUMER_GROUP, CONSUMER_NAME,
                    {stream: ">"}, count=10, block=5000,
                )

            if not results:
                await asyncio.sleep(0.5)
                continue

            for stream_name, messages in results:
                for msg_id, data in messages:
                    try:
                        score_data = {}
                        for k, v in data.items():
                            try:
                                score_data[k] = json.loads(v)
                            except (json.JSONDecodeError, TypeError):
                                score_data[k] = v

                        # scores are stored as {"data": {actual payload}}
                        if "data" in score_data and isinstance(score_data["data"], dict):
                            score_data = score_data["data"]

                        features = score_data.get("features", {})
                        if features and not score_data.get("anomaly_type"):
                            score_data["anomaly_type"] = engine.classify_anomaly(features)

                        active_rules = loader.get_active_rules()
                        alerts = await engine.evaluate(score_data, active_rules)

                        for alert in alerts:
                            alert_data = {
                                k: json.dumps(v) if isinstance(v, (dict, list)) else str(v)
                                for k, v in alert.items()
                            }
                            await client.xadd(alert_stream, alert_data, maxlen=10000, approximate=True)
                            await client.set("sentinel:latest_alert", json.dumps(alert), ex=3600)
                            await client.incr("sentinel:alert_count")

                            await persist_alert_to_db(db_pool, alert, client)
                            await client.xadd(
                                audit_stream,
                                {
                                    "type": "alert_generated",
                                    "alert_id": alert["alert_id"],
                                    "policy": alert["policy_name"],
                                    "severity": alert["severity"],
                                    "score": str(alert["score"]),
                                    "action": alert["action"],
                                    "timestamp": alert["timestamp"],
                                },
                                maxlen=100000, approximate=True,
                            )
                            if alert["action"] != "alert_only":
                                await client.xadd(
                                    "sentinel:action_requests",
                                    {
                                        "alert_id": alert["alert_id"],
                                        "action": alert["action"],
                                        "target": alert.get("source_ip", ""),
                                        "triggered_by": "policy",
                                        "policy_name": alert["policy_name"],
                                        "severity": alert["severity"],
                                        "timestamp": alert["timestamp"],
                                    },
                                    maxlen=5000, approximate=True,
                                )

                        await client.xack(stream, CONSUMER_GROUP, msg_id)

                    except Exception as e:
                        logger.error("score_processing_error", msg_id=msg_id, error=str(e))
                        try:
                            await client.xadd(
                                "sentinel:dead_letter",
                                {"source": "policy", "msg_id": msg_id, "reason": str(e)[:200],
                                 "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
                                maxlen=1000, approximate=True,
                            )
                        except Exception:
                            pass
                        await client.xack(stream, CONSUMER_GROUP, msg_id)

            # Yield CPU after processing a batch to prevent tight loop
            await asyncio.sleep(0.1)

        except aioredis.ConnectionError:
            logger.error("policy_redis_connection_lost")
            mark_unhealthy()
            await asyncio.sleep(5)
        except Exception as e:
            logger.error("policy_process_error", error=str(e))
            await asyncio.sleep(2)


async def main() -> None:
    logger.info("policy_engine_starting", version="v0.1", redis_url=_mask_url(REDIS_URL), db_url=_mask_url(DB_URL[:60]) if DB_URL else "not_set")

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, handle_signal, sig)

    loader = PolicyLoader("/config/policies.yml")
    loader.load()
    loader.start_watching()

    client = await connect_redis()
    db_pool = await connect_db()
    engine = PolicyEngine(redis_client=client)
    mark_healthy()

    tasks = [
        asyncio.create_task(heartbeat(client)),
        asyncio.create_task(process_scores(client, engine, loader, db_pool)),
    ]

    logger.info(
        "policy_engine_active",
        rules_loaded=len(loader.rules),
        active_rules=len(loader.get_active_rules()),
        db_connected=db_pool is not None,
    )

    await shutdown_event.wait()

    logger.info("policy_engine_shutting_down")
    loader.stop_watching()
    mark_unhealthy()

    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await client.aclose()
    if db_pool:
        await db_pool.close()
    logger.info("policy_engine_stopped")


if __name__ == "__main__":
    asyncio.run(main())
