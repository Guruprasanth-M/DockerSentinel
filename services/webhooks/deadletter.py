"""Dead-letter queue for failed webhooks."""
import json
import time
import structlog

logger = structlog.get_logger("hostspectra.webhooks.queue")

DEAD_LETTER_STREAM = "sentinel:webhooks:failed"
MAX_DEAD_LETTERS = 1000


async def add_to_dead_letter(
    redis_client,
    webhook_name: str,
    url: str,
    payload: dict,
    error: str,
    attempts: int,
):
    """
    Add a permanently failed webhook delivery to the dead letter queue.
    
    Args:
        redis_client: Redis async client
        webhook_name: Name of the webhook config
        url: Target URL
        payload: Original alert payload
        error: Last error message
        attempts: Total delivery attempts made
    """
    try:
        entry = {
            "webhook_name": webhook_name,
            "url": url,
            "payload": json.dumps(payload),
            "error": str(error),
            "attempts": str(attempts),
            "failed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

        await redis_client.xadd(
            DEAD_LETTER_STREAM,
            entry,
            maxlen=MAX_DEAD_LETTERS,
            approximate=True,
        )

        logger.warning(
            "webhook_dead_lettered",
            webhook=webhook_name,
            url=url,
            attempts=attempts,
            error=error,
        )

    except Exception as e:
        logger.error("dead_letter_write_failed", error=str(e))


async def get_dead_letters(redis_client, count: int = 50) -> list:
    """Retrieve recent dead letter entries."""
    try:
        entries = await redis_client.xrevrange(
            DEAD_LETTER_STREAM, count=count,
        )
        result = []
        for msg_id, data in entries:
            entry = dict(data)
            entry["id"] = msg_id
            if "payload" in entry:
                try:
                    entry["payload"] = json.loads(entry["payload"])
                except (json.JSONDecodeError, TypeError):
                    pass
            result.append(entry)
        return result
    except Exception as e:
        logger.error("dead_letter_read_failed", error=str(e))
        return []


async def clear_dead_letters(redis_client) -> int:
    """Clear all dead letter entries. Returns count deleted."""
    try:
        length = await redis_client.xlen(DEAD_LETTER_STREAM)
        await redis_client.delete(DEAD_LETTER_STREAM)
        logger.info("dead_letters_cleared", count=length)
        return length
    except Exception as e:
        logger.error("dead_letter_clear_failed", error=str(e))
        return 0
