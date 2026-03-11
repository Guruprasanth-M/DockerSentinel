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
                "sentinel:heartbeat:webhook_service",
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
    stream = "sentinel:alerts"
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
                                "sentinel:webhook_stats",
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


async def main() -> None:
    logger.info("webhook_service_starting", version="v0.2", redis_url=_mask_url(REDIS_URL))

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, handle_signal, sig)

    config = WebhookConfig("/config/webhooks.yml")
    config.load()
    config.load_host_name()

    client = await connect_redis()
    mark_healthy()

    tasks = [
        asyncio.create_task(heartbeat(client)),
        asyncio.create_task(process_alerts(client, config)),
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

    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await close_http_client()
    await client.aclose()
    logger.info("webhook_service_stopped")


if __name__ == "__main__":
    asyncio.run(main())
