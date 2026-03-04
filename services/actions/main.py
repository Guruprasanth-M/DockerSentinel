"""Action executor service."""
import asyncio
import json
import os
import signal
import time
from pathlib import Path

import re

import redis.asyncio as aioredis
import structlog
import yaml

from whitelist import validate_action, is_ip_protected, is_process_protected
from ip_block import block_ip, list_blocked_ips
from process_manager import kill_process, get_process_info
from reversal import ReversalScheduler

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer(),
    ]
)

logger = structlog.get_logger("sentinel.actions")

def _mask_url(url: str) -> str:
    """Mask passwords in connection URLs for safe logging."""
    return re.sub(r'(://[^:]*:)[^@]+(@)', r'\1*****\2', url)


REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
HEARTBEAT_INTERVAL = 30
CONSUMER_GROUP = "action_engine"
CONSUMER_NAME = f"actions_{os.getpid()}"
HEALTH_FILE = "/tmp/actions_healthy"

shutdown_event = asyncio.Event()


class RateLimiter:
    """Per-target sliding window rate limiter for actions.
    
    Tracks action timestamps per target IP/PID so one attacker
    cannot exhaust the budget for all others (BUG-C07 fix).
    Stale entries are cleaned on every call to prevent memory leaks.
    """
    
    def __init__(self, max_per_minute: int = 5):
        self.max_per_minute = max_per_minute
        self._targets: dict[str, list[float]] = {}
    
    def allow(self, target: str = "__global__") -> bool:
        now = time.time()
        cutoff = now - 60
        # Clean stale entries across all targets
        self._cleanup(cutoff)
        timestamps = self._targets.get(target, [])
        timestamps = [t for t in timestamps if t > cutoff]
        if len(timestamps) >= self.max_per_minute:
            self._targets[target] = timestamps
            return False
        timestamps.append(now)
        self._targets[target] = timestamps
        return True
    
    def remaining(self, target: str = "__global__") -> int:
        now = time.time()
        cutoff = now - 60
        timestamps = [t for t in self._targets.get(target, []) if t > cutoff]
        return max(0, self.max_per_minute - len(timestamps))

    def _cleanup(self, cutoff: float) -> None:
        """Remove targets with no recent timestamps to prevent memory leaks."""
        stale = [k for k, v in self._targets.items() if not any(t > cutoff for t in v)]
        for k in stale:
            del self._targets[k]


def handle_signal(sig: int) -> None:
    logger.info("actions_signal_received", signal=sig)
    shutdown_event.set()


def mark_healthy():
    Path(HEALTH_FILE).touch()


def mark_unhealthy():
    try:
        Path(HEALTH_FILE).unlink(missing_ok=True)
    except Exception:
        pass


def load_config() -> dict:
    config_path = os.environ.get("SENTINEL_CONFIG", "/config/sentinel.yml")
    try:
        with open(config_path, "r") as f:
            data = yaml.safe_load(f) or {}
        return data.get("sentinel", {}).get("actions", {})
    except Exception as e:
        logger.warning("config_load_failed", error=str(e))
        return {}


async def connect_redis() -> aioredis.Redis:
    max_retries = 30
    for attempt in range(1, max_retries + 1):
        try:
            client = aioredis.from_url(REDIS_URL, decode_responses=True)
            await client.ping()
            logger.info("actions_redis_connected", attempt=attempt)
            return client
        except Exception as e:
            wait = min(2 ** attempt, 60)
            logger.warning("actions_redis_retry", attempt=attempt, wait=wait, error=str(e))
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
                "sentinel:heartbeat:action_engine",
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


async def process_action_requests(
    client: aioredis.Redis,
    config: dict,
    rate_limiter: RateLimiter,
    reversal_scheduler: ReversalScheduler,
):
    stream = "sentinel:action_requests"
    action_stream = "sentinel:actions"
    audit_stream = "sentinel:audit"

    await ensure_consumer_group(client, stream, CONSUMER_GROUP)

    actions_enabled = config.get("enabled", True)
    protected_ips = config.get("protected_ips", ["127.0.0.1", "::1"])
    default_block_duration = config.get("default_block_duration_minutes", 60)

    logger.info(
        "action_engine_processing",
        enabled=actions_enabled,
        max_per_minute=rate_limiter.max_per_minute,
    )

    last_id = "0"
    processed_pending = False

    while not shutdown_event.is_set():
        try:
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
                        action = data.get("action", "")
                        target = data.get("target", "")
                        triggered_by = data.get("triggered_by", "policy")
                        alert_id = data.get("alert_id", "")
                        action_id = f"act_{int(time.time())}_{msg_id}"
                        timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

                        # Check if actions are enabled
                        if not actions_enabled:
                            logger.info("action_disabled", action=action, target=target)
                            result = {
                                "status": "disabled",
                                "message": "Actions are disabled in configuration",
                            }
                        # Check rate limit per target
                        elif not rate_limiter.allow(target):
                            logger.warning("action_rate_limited", action=action, target=target,
                                           remaining=rate_limiter.remaining(target))
                            result = {
                                "status": "rate_limited",
                                "message": f"Rate limit exceeded for {target} ({rate_limiter.max_per_minute}/min)",
                            }
                        else:
                            # Validate action against whitelist
                            allowed, reason = validate_action(action, target, protected_ips)
                            if not allowed:
                                logger.warning("action_rejected", action=action, target=target, reason=reason)
                                result = {"status": "rejected", "message": reason}
                            else:
                                # Execute action
                                result = await execute_action(action, target)

                                # Schedule reversal if applicable
                                if result.get("status") == "blocked" and action == "block_ip":
                                    await reversal_scheduler.schedule_reversal(
                                        action_id, action, target, default_block_duration,
                                    )

                        # Publish action result
                        await client.xadd(
                            action_stream,
                            {
                                "action_id": action_id,
                                "action": action,
                                "target": target,
                                "triggered_by": triggered_by,
                                "alert_id": alert_id,
                                "status": result.get("status", "unknown"),
                                "message": result.get("message", ""),
                                "reversible": str(action == "block_ip"),
                                "timestamp": timestamp,
                            },
                            maxlen=5000, approximate=True,
                        )

                        # Audit log
                        await client.xadd(
                            audit_stream,
                            {
                                "type": "action_executed",
                                "action_id": action_id,
                                "action": action,
                                "target": target,
                                "triggered_by": triggered_by,
                                "status": result.get("status", "unknown"),
                                "timestamp": timestamp,
                            },
                            maxlen=100000, approximate=True,
                        )

                        await client.xack(stream, CONSUMER_GROUP, msg_id)

                    except Exception as e:
                        logger.error("action_processing_error", msg_id=msg_id, error=str(e))
                        try:
                            await client.xadd(
                                "sentinel:dead_letter",
                                {"source": "actions", "msg_id": msg_id, "reason": str(e)[:200],
                                 "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
                                maxlen=1000, approximate=True,
                            )
                        except Exception:
                            pass
                        await client.xack(stream, CONSUMER_GROUP, msg_id)

            # Yield CPU after processing a batch to prevent tight loop
            await asyncio.sleep(0.1)

        except aioredis.ConnectionError:
            logger.error("actions_redis_connection_lost")
            mark_unhealthy()
            await asyncio.sleep(5)
        except Exception as e:
            logger.error("actions_process_error", error=str(e))
            await asyncio.sleep(2)


async def execute_action(action: str, target: str) -> dict:
    """Execute a validated action.
    
    Defense-in-depth: re-checks whitelist even though validate_action()
    is called upstream, in case execute_action is called from a new path.
    """
    if action == "block_ip":
        if is_ip_protected(target):
            return {"status": "rejected", "message": f"IP {target} is protected (whitelist)"}
        return await block_ip(target, reason="policy_triggered")
    elif action == "kill_process":
        try:
            pid = int(target)
            # Resolve process name and check against protected list
            info = get_process_info(pid)
            if is_process_protected(info.get("name", "")):
                return {"status": "rejected", "message": f"Process '{info['name']}' (PID {pid}) is protected"}
            return await kill_process(pid, process_name=info.get("name", ""))
        except ValueError:
            return {"status": "failed", "message": f"Invalid PID: {target}"}
    elif action == "alert_only":
        return {"status": "alert_only", "message": "Alert logged, no action taken"}
    else:
        return {"status": "unknown_action", "message": f"Unknown action: {action}"}


async def main() -> None:
    logger.info("action_engine_starting", version="0.1.0", redis_url=_mask_url(REDIS_URL))

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, handle_signal, sig)

    config = load_config()
    max_per_minute = config.get("max_per_minute", 5)

    rate_limiter = RateLimiter(max_per_minute)
    client = await connect_redis()
    reversal_scheduler = ReversalScheduler(redis_client=client)
    await reversal_scheduler.resume_pending()

    mark_healthy()

    tasks = [
        asyncio.create_task(heartbeat(client)),
        asyncio.create_task(
            process_action_requests(client, config, rate_limiter, reversal_scheduler)
        ),
    ]

    logger.info(
        "action_engine_active",
        actions_enabled=config.get("enabled", True),
        max_per_minute=max_per_minute,
    )

    await shutdown_event.wait()

    logger.info("action_engine_shutting_down")
    mark_unhealthy()
    await reversal_scheduler.shutdown()

    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await client.aclose()
    logger.info("action_engine_stopped")


if __name__ == "__main__":
    asyncio.run(main())
