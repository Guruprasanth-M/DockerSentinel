"""Webhook dispatcher service."""
import asyncio
import json
import os
import re
import signal
import time
from pathlib import Path

import redis.asyncio as aioredis
import structlog
import yaml

from dispatcher import dispatch_webhook, format_alert_payload, close_http_client
from signer import sign_payload
from deadletter import add_to_dead_letter

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer(),
    ]
)

logger = structlog.get_logger("hostspectra.webhooks")

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")


def _mask_url(url: str) -> str:
    """Mask passwords in connection URLs for safe logging."""
    return re.sub(r'(://[^:]*:)[^@]+(@)', r'\1*****\2', url)


HEARTBEAT_INTERVAL = 30
CONSUMER_GROUP = "webhook_service"
CONSUMER_NAME = f"webhooks_{os.getpid()}"
HEALTH_FILE = "/tmp/webhooks_healthy"
REVERSAL_REQUEST_STREAM = "hostspectra:reversal_requests"
REVERSAL_GROUP = "reversal_scheduler"
REVERSAL_CONSUMER = f"reversal_{os.getpid()}"
REVERSAL_KEY = "hostspectra:reversals"
REVERSAL_META_PREFIX = "hostspectra:reversal_meta"

shutdown_event = asyncio.Event()


class WebhookConfig:
    """Manages webhook configuration with reload support."""
    
    def __init__(self, config_path: str = "/config/webhooks.yml"):
        self.config_path = config_path
        self.webhooks: list = []
        self.webhook_secret: str = ""
        self.host_name: str = ""
        self._last_modified: float = 0
    
    def load(self):
        try:
            if not os.path.exists(self.config_path):
                logger.warning("webhook_config_not_found", path=self.config_path)
                return
            
            with open(self.config_path, "r") as f:
                data = yaml.safe_load(f) or {}
            
            self.webhooks = data.get("webhooks", [])
            self.webhook_secret = data.get("webhook_secret", "")
            self._last_modified = os.path.getmtime(self.config_path)
            
            enabled_count = sum(1 for w in self.webhooks if w.get("enabled", False))
            logger.info(
                "webhooks_loaded",
                total=len(self.webhooks),
                enabled=enabled_count,
            )
        except Exception as e:
            logger.error("webhook_config_load_error", error=str(e))
    
    def load_host_name(self):
        config_path = os.environ.get("HOSTSPECTRA_CONFIG", "/config/hostspectra.yml")
        try:
            with open(config_path, "r") as f:
                data = yaml.safe_load(f) or {}
            self.host_name = data.get("hostspectra", {}).get("host_name", "")
        except Exception:
            self.host_name = os.environ.get("HOSTNAME", "hostspectra")
    
    def check_reload(self):
        try:
            if os.path.exists(self.config_path):
                mtime = os.path.getmtime(self.config_path)
                if mtime > self._last_modified:
                    logger.info("webhook_config_changed")
                    self.load()
        except Exception:
            pass
    
    def get_matching_webhooks(self, event_type: str) -> list:
        matching = []
        for wh in self.webhooks:
            if not wh.get("enabled", False):
                continue
            events = wh.get("events", [])
            if event_type in events or not events:
                matching.append(wh)
        return matching


def handle_signal(sig: int) -> None:
    logger.info("webhooks_signal_received", signal=sig)
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
            logger.info("webhooks_redis_connected", attempt=attempt)
            return client
        except Exception as e:
            wait = min(2 ** attempt, 60)
            logger.warning("webhooks_redis_retry", attempt=attempt, wait=wait, error=str(e))
            await asyncio.sleep(wait)
    raise ConnectionError("Failed to connect to Redis")


async def ensure_consumer_group(client: aioredis.Redis, stream: str, group: str):
    try:
        await client.xgroup_create(stream, group, id="0", mkstream=True)
    except aioredis.ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise


async def heartbeat(client: aioredis.Redis) -> None:
    while not shutdown_event.is_set():
        try:
            await client.set(
                "hostspectra:heartbeat:webhook_service",
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


async def process_alerts(client: aioredis.Redis, config: WebhookConfig):
    stream = "hostspectra:alerts"
    await ensure_consumer_group(client, stream, CONSUMER_GROUP)

    logger.info("webhook_processor_started", stream=stream, consumer=CONSUMER_NAME)

    last_id = "0"
    processed_pending = False
    config_check_counter = 0

    while not shutdown_event.is_set():
        try:
            # Periodically check for config reload
            config_check_counter += 1
            if config_check_counter % 10 == 0:
                config.check_reload()

            if not processed_pending:
                results = await client.xreadgroup(
                    CONSUMER_GROUP, CONSUMER_NAME,
                    {stream: last_id}, count=5, block=1000,
                )
                has_msgs = results and any(msgs for _, msgs in results)
                if not has_msgs:
                    processed_pending = True
                    continue
            else:
                results = await client.xreadgroup(
                    CONSUMER_GROUP, CONSUMER_NAME,
                    {stream: ">"}, count=5, block=5000,
                )

            if not results:
                await asyncio.sleep(0.5)
                continue

            for stream_name, messages in results:
                for msg_id, data in messages:
                    try:
                        # Parse alert data
                        alert = {}
                        for k, v in data.items():
                            try:
                                alert[k] = json.loads(v)
                            except (json.JSONDecodeError, TypeError):
                                alert[k] = v

                        # Check if notification is requested
                        notify = alert.get("notify", "true")
                        if str(notify).lower() in ("false", "0", "no"):
                            await client.xack(stream, CONSUMER_GROUP, msg_id)
                            continue

                        # Format payload
                        payload = format_alert_payload(alert, config.host_name)
                        event_type = payload.get("event_type", "anomaly_detected")

                        # Find matching webhooks
                        matching = config.get_matching_webhooks(event_type)

                        if not matching:
                            logger.debug(
                                "no_matching_webhooks",
                                event_type=event_type,
                                alert_id=alert.get("alert_id"),
                            )
                            await client.xack(stream, CONSUMER_GROUP, msg_id)
                            continue

                        # Dispatch to all matching webhooks
                        for wh in matching:
                            result = await dispatch_webhook(
                                wh, payload, config.webhook_secret,
                            )

                            if result["status"] == "failed":
                                await add_to_dead_letter(
                                    client,
                                    webhook_name=wh.get("name", "unknown"),
                                    url=wh.get("url", ""),
                                    payload=payload,
                                    error=result.get("error", ""),
                                    attempts=result.get("attempts", 0),
                                )

                            # Track delivery stats
                            await client.hincrby(
                                "hostspectra:webhook_stats",
                                f"{wh.get('name', 'unknown')}:{result['status']}",
                                1,
                            )

                        await client.xack(stream, CONSUMER_GROUP, msg_id)

                    except Exception as e:
                        logger.error("webhook_processing_error", msg_id=msg_id, error=str(e))
                        await client.xack(stream, CONSUMER_GROUP, msg_id)

            # Yield CPU after processing a batch to prevent tight loop
            await asyncio.sleep(0.1)

        except aioredis.ConnectionError:
            logger.error("webhooks_redis_connection_lost")
            mark_unhealthy()
            await asyncio.sleep(5)
        except Exception as e:
            logger.error("webhooks_process_error", error=str(e))
            await asyncio.sleep(2)


class ReversalSchedulerWorker:
    """Runs reversal timing outside actions container (BUG-M11 fix)."""

    def __init__(self, client: aioredis.Redis):
        self._client = client
        self._tasks: dict[str, asyncio.Task] = {}

    @staticmethod
    def _meta_key(action_id: str) -> str:
        return f"{REVERSAL_META_PREFIX}:{action_id}"

    def _schedule_task(self, action_id: str, target: str, delay_seconds: float) -> None:
        task = self._tasks.get(action_id)
        if task and not task.done():
            return
        self._tasks[action_id] = asyncio.create_task(
            self._dispatch_unblock(action_id, target, delay_seconds)
        )

    async def _dispatch_unblock(self, action_id: str, target: str, delay_seconds: float) -> None:
        try:
            await asyncio.sleep(delay_seconds)
            await self._client.xadd(
                "hostspectra:action_requests",
                {
                    "action": "unblock_ip",
                    "target": target,
                    "triggered_by": "reversal_scheduler",
                    "alert_id": "",
                },
                maxlen=5000,
                approximate=True,
            )
            await self._client.zrem(REVERSAL_KEY, action_id)
            await self._client.delete(self._meta_key(action_id))
            logger.info("reversal_dispatched", action_id=action_id, target=target)
        except asyncio.CancelledError:
            logger.info("reversal_cancelled", action_id=action_id)
            raise
        except Exception as e:
            logger.error("reversal_dispatch_failed", action_id=action_id, error=str(e))
        finally:
            self._tasks.pop(action_id, None)

    async def schedule(self, action_id: str, target: str, duration_minutes: int) -> None:
        reversal_at = time.time() + (duration_minutes * 60)
        await self._client.zadd(REVERSAL_KEY, {action_id: reversal_at})
        await self._client.hset(
            self._meta_key(action_id),
            mapping={
                "action_id": action_id,
                "target": target,
                "duration_minutes": str(duration_minutes),
                "scheduled_at": str(time.time()),
            },
        )
        await self._client.expire(self._meta_key(action_id), max(3600, duration_minutes * 120))

        delay_seconds = max(0.0, reversal_at - time.time())
        self._schedule_task(action_id, target, delay_seconds)
        logger.info(
            "reversal_scheduled",
            action_id=action_id,
            target=target,
            duration_minutes=duration_minutes,
            delay_seconds=int(delay_seconds),
        )

    async def resume_pending(self) -> None:
        now = time.time()
        entries = await self._client.zrangebyscore(REVERSAL_KEY, "-inf", "+inf", withscores=True)
        for action_id, reversal_at in entries:
            meta = await self._client.hgetall(self._meta_key(action_id))
            target = meta.get("target", "") if meta else ""
            if not target:
                await self._client.zrem(REVERSAL_KEY, action_id)
                await self._client.delete(self._meta_key(action_id))
                continue
            delay_seconds = max(0.0, reversal_at - now)
            self._schedule_task(action_id, target, delay_seconds)
            logger.info("reversal_resumed", action_id=action_id, remaining_seconds=int(delay_seconds))

    async def shutdown(self) -> None:
        for task in list(self._tasks.values()):
            if not task.done():
                task.cancel()
        self._tasks.clear()


async def process_reversal_requests(client: aioredis.Redis, scheduler: ReversalSchedulerWorker):
    await ensure_consumer_group(client, REVERSAL_REQUEST_STREAM, REVERSAL_GROUP)

    processed_pending = False
    while not shutdown_event.is_set():
        try:
            if not processed_pending:
                results = await client.xreadgroup(
                    REVERSAL_GROUP, REVERSAL_CONSUMER,
                    {REVERSAL_REQUEST_STREAM: "0"}, count=20, block=1000,
                )
                has_msgs = results and any(msgs for _, msgs in results)
                if not has_msgs:
                    processed_pending = True
                    continue
            else:
                results = await client.xreadgroup(
                    REVERSAL_GROUP, REVERSAL_CONSUMER,
                    {REVERSAL_REQUEST_STREAM: ">"}, count=20, block=5000,
                )

            if not results:
                await asyncio.sleep(0.2)
                continue

            for _, messages in results:
                for msg_id, data in messages:
                    try:
                        action_id = data.get("action_id", "")
                        action = data.get("action", "")
                        target = data.get("target", "")
                        duration_minutes = int(data.get("duration_minutes", "0"))

                        if not action_id or action != "block_ip" or not target or duration_minutes <= 0:
                            logger.warning(
                                "invalid_reversal_request",
                                msg_id=msg_id,
                                action_id=action_id,
                                action=action,
                                target=target,
                                duration_minutes=duration_minutes,
                            )
                            await client.xack(REVERSAL_REQUEST_STREAM, REVERSAL_GROUP, msg_id)
                            continue

                        await scheduler.schedule(action_id, target, duration_minutes)
                        await client.xack(REVERSAL_REQUEST_STREAM, REVERSAL_GROUP, msg_id)
                    except Exception as e:
                        logger.error("reversal_request_error", msg_id=msg_id, error=str(e))
                        await asyncio.sleep(0.1)

            await asyncio.sleep(0.05)
        except aioredis.ConnectionError:
            logger.error("reversal_scheduler_redis_connection_lost")
            mark_unhealthy()
            await asyncio.sleep(5)
        except Exception as e:
            logger.error("reversal_scheduler_error", error=str(e))
            await asyncio.sleep(2)


async def main() -> None:
    logger.info("webhook_service_starting", version="v0.2", redis_url=_mask_url(REDIS_URL))

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, handle_signal, sig)

    config = WebhookConfig("/config/webhooks.yml")
    config.load()
    config.load_host_name()

    client = await connect_redis()
    reversal_scheduler = ReversalSchedulerWorker(client)
    await reversal_scheduler.resume_pending()
    mark_healthy()

    tasks = [
        asyncio.create_task(heartbeat(client)),
        asyncio.create_task(process_alerts(client, config)),
        asyncio.create_task(process_reversal_requests(client, reversal_scheduler)),
    ]

    enabled_count = sum(1 for w in config.webhooks if w.get("enabled", False))
    logger.info(
        "webhook_service_active",
        webhooks_configured=len(config.webhooks),
        webhooks_enabled=enabled_count,
    )

    await shutdown_event.wait()

    logger.info("webhook_service_shutting_down")
    mark_unhealthy()
    await reversal_scheduler.shutdown()

    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await close_http_client()
    await client.aclose()
    logger.info("webhook_service_stopped")


if __name__ == "__main__":
    asyncio.run(main())
