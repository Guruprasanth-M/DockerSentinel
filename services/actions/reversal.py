"""Scheduled auto-reversal of actions — H5: Redis-backed persistence."""
import asyncio
import json
import time
import structlog
from typing import Dict, Any

from ip_block import unblock_ip

logger = structlog.get_logger("sentinel.actions.reversal")

REVERSAL_KEY = "sentinel:reversals"


class ReversalScheduler:
    """
    Manages scheduled reversals of actions (e.g., timed IP blocks).
    
    H5 fix: Stores reversal schedule in Redis sorted set (score = reversal_timestamp)
    so that in-flight reversals survive service restarts.
    """

    def __init__(self, redis_client=None):
        self._tasks: Dict[str, asyncio.Task] = {}
        self._redis = redis_client

    async def schedule_reversal(
        self,
        action_id: str,
        action: str,
        target: str,
        duration_minutes: int,
    ):
        """Schedule a reversal for a timed action."""
        reversal_at = time.time() + (duration_minutes * 60)

        entry = json.dumps({
            "action_id": action_id,
            "action": action,
            "target": target,
            "scheduled_at": time.time(),
            "duration_minutes": duration_minutes,
        })

        # Persist to Redis sorted set (score = reversal timestamp)
        if self._redis:
            try:
                await self._redis.zadd(REVERSAL_KEY, {entry: reversal_at})
            except Exception as e:
                logger.error("reversal_persist_failed", error=str(e))

        task = asyncio.create_task(
            self._execute_reversal(action_id, action, target, duration_minutes * 60, entry)
        )
        self._tasks[action_id] = task

        logger.info(
            "reversal_scheduled",
            action_id=action_id,
            action=action,
            target=target,
            duration_minutes=duration_minutes,
        )

    async def _execute_reversal(
        self, action_id: str, action: str, target: str, delay_seconds: float, redis_member: str = ""
    ):
        """Wait for the specified duration, then reverse the action."""
        try:
            await asyncio.sleep(delay_seconds)

            if action == "block_ip":
                result = await unblock_ip(target)
                logger.info(
                    "reversal_executed",
                    action_id=action_id,
                    action=action,
                    target=target,
                    result=result.get("status"),
                )

                # Log to audit stream
                if self._redis:
                    try:
                        await self._redis.xadd(
                            "sentinel:audit",
                            {
                                "type": "action_reversed",
                                "action_id": action_id,
                                "action": action,
                                "target": target,
                                "status": result.get("status", "unknown"),
                                "timestamp": time.strftime(
                                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                                ),
                            },
                            maxlen=100000,
                            approximate=True,
                        )
                    except Exception as e:
                        logger.error("reversal_audit_failed", error=str(e))

            # Remove from Redis
            if self._redis and redis_member:
                try:
                    await self._redis.zrem(REVERSAL_KEY, redis_member)
                except Exception:
                    pass

            self._tasks.pop(action_id, None)

        except asyncio.CancelledError:
            logger.info("reversal_cancelled", action_id=action_id)
            self._tasks.pop(action_id, None)
        except Exception as e:
            logger.error(
                "reversal_failed",
                action_id=action_id,
                error=str(e),
            )

    def cancel_reversal(self, action_id: str) -> bool:
        """Cancel a scheduled reversal."""
        task = self._tasks.pop(action_id, None)
        if task and not task.done():
            task.cancel()
            logger.info("reversal_cancelled", action_id=action_id)
            return True
        return False

    async def resume_pending(self):
        """H5: On startup, resume any reversals saved in Redis."""
        if not self._redis:
            return
        try:
            now = time.time()
            entries = await self._redis.zrangebyscore(REVERSAL_KEY, "-inf", "+inf", withscores=True)
            for member, reversal_at in entries:
                info = json.loads(member)
                remaining = max(0, reversal_at - now)
                action_id = info["action_id"]

                if remaining <= 0:
                    # Overdue — execute immediately
                    logger.info("reversal_overdue_executing", action_id=action_id, target=info["target"])
                    remaining = 0

                task = asyncio.create_task(
                    self._execute_reversal(action_id, info["action"], info["target"], remaining, member)
                )
                self._tasks[action_id] = task
                logger.info("reversal_resumed", action_id=action_id, remaining_seconds=int(remaining))
        except Exception as e:
            logger.error("reversal_resume_failed", error=str(e))

    def get_pending(self) -> list:
        """Get all pending reversals (from in-memory tasks)."""
        return [
            {"action_id": aid, "active": not task.done()}
            for aid, task in self._tasks.items()
        ]

    async def shutdown(self):
        """Cancel all pending reversal tasks (Redis entries persist for next startup)."""
        for action_id, task in list(self._tasks.items()):
            if not task.done():
                task.cancel()
        self._tasks.clear()
        logger.info("reversal_scheduler_shutdown")
