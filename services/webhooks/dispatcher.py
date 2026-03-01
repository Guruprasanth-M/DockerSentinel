"""HTTP delivery of webhook payloads."""
import asyncio
import json
import time
import structlog
from typing import Dict, Any, Optional

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

try:
    import urllib.request
    import urllib.error
    HAS_URLLIB = True
except ImportError:
    HAS_URLLIB = False

from signer import sign_payload

logger = structlog.get_logger("sentinel.webhooks.dispatcher")

# Retry schedule: 1s, 2s, 4s, 8s, 16s
MAX_RETRIES = 5
RETRY_DELAYS = [1, 2, 4, 8, 16]

# H6: Shared HTTP client with connection pooling
_http_client: Optional["httpx.AsyncClient"] = None


async def get_http_client() -> "httpx.AsyncClient":
    """Get or create shared httpx.AsyncClient with connection pooling."""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=10.0,
            verify=True,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _http_client


async def close_http_client():
    """Close the shared HTTP client. Call on service shutdown."""
    global _http_client
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()
        _http_client = None


async def dispatch_webhook(
    webhook_config: dict,
    payload: dict,
    webhook_secret: str = "",
) -> dict:
    """
    Dispatch a webhook payload to the configured URL.
    
    Uses exponential backoff retry on failure.
    
    Args:
        webhook_config: Webhook configuration dict (name, url, headers, sign_payloads)
        payload: Alert payload to send
        webhook_secret: Secret for HMAC-SHA256 signing
        
    Returns:
        Result dict with status, attempts, and any error
    """
    url = webhook_config.get("url", "")
    name = webhook_config.get("name", "unknown")
    custom_headers = webhook_config.get("headers", {})
    sign = webhook_config.get("sign_payloads", False)

    if not url:
        return {"status": "error", "message": "No URL configured", "attempts": 0}

    # Prepare payload
    body = json.dumps(payload, default=str).encode("utf-8")

    # Build headers
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "DockerSentinel/0.1.0",
        "X-Sentinel-Event": payload.get("event_type", "unknown"),
        "X-Sentinel-Delivery-Id": payload.get("alert_id", ""),
    }

    # Add custom headers
    if custom_headers:
        headers.update(custom_headers)

    # Sign payload if configured
    if sign and webhook_secret:
        signature = sign_payload(body, webhook_secret)
        headers["X-Sentinel-Signature"] = signature

    # Attempt delivery with retries
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            status_code = await _send_request(url, body, headers)

            if 200 <= status_code < 300:
                logger.info(
                    "webhook_delivered",
                    webhook=name,
                    url=url,
                    status=status_code,
                    attempt=attempt,
                )
                return {
                    "status": "delivered",
                    "status_code": status_code,
                    "attempts": attempt,
                }

            # Non-2xx response
            last_error = f"HTTP {status_code}"
            logger.warning(
                "webhook_non_2xx",
                webhook=name,
                status=status_code,
                attempt=attempt,
            )

        except asyncio.TimeoutError:
            last_error = "Request timed out"
            logger.warning("webhook_timeout", webhook=name, attempt=attempt)
        except Exception as e:
            last_error = str(e)
            logger.warning(
                "webhook_delivery_failed",
                webhook=name,
                attempt=attempt,
                error=last_error,
            )

        # Wait before retry
        if attempt < MAX_RETRIES:
            delay = RETRY_DELAYS[attempt - 1]
            await asyncio.sleep(delay)

    # All retries exhausted
    logger.error(
        "webhook_permanently_failed",
        webhook=name,
        url=url,
        attempts=MAX_RETRIES,
        last_error=last_error,
    )

    return {
        "status": "failed",
        "attempts": MAX_RETRIES,
        "error": last_error,
    }


async def _send_request(url: str, body: bytes, headers: dict) -> int:
    """Send HTTP POST request. Uses httpx if available, falls back to urllib."""
    if HAS_HTTPX:
        return await _send_httpx(url, body, headers)
    elif HAS_URLLIB:
        return await _send_urllib(url, body, headers)
    else:
        raise RuntimeError("No HTTP library available (install httpx or use stdlib)")


async def _send_httpx(url: str, body: bytes, headers: dict) -> int:
    """Send request using httpx with connection pooling (H6)."""
    client = await get_http_client()
    response = await client.post(url, content=body, headers=headers)
    return response.status_code


async def _send_urllib(url: str, body: bytes, headers: dict) -> int:
    """Send request using urllib (sync, run in executor)."""
    loop = asyncio.get_running_loop()

    def _do_request():
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            resp = urllib.request.urlopen(req, timeout=10)
            return resp.getcode()
        except urllib.error.HTTPError as e:
            return e.code

    return await loop.run_in_executor(None, _do_request)


def format_alert_payload(alert: dict, host_name: str = "") -> dict:
    """
    Format an alert into a webhook payload with standard fields.
    
    Returns a payload suitable for Slack, Discord, or generic webhooks.
    """
    severity = alert.get("severity", "medium")
    score = alert.get("score", 0.0)
    
    severity_emoji = {
        "low": "🟢",
        "medium": "🟡",
        "high": "🟠",
        "critical": "🔴",
    }
    
    emoji = severity_emoji.get(severity, "⚪")

    return {
        "event_type": _classify_event_type(alert),
        "alert_id": alert.get("alert_id", ""),
        "timestamp": alert.get("timestamp", ""),
        "severity": severity,
        "score": score,
        "risk_level": alert.get("risk_level", ""),
        "policy_name": alert.get("policy_name", ""),
        "anomaly_type": alert.get("anomaly_type", ""),
        "source_ip": alert.get("source_ip", ""),
        "action": alert.get("action", "alert_only"),
        "message": alert.get("message", ""),
        "host": host_name,
        # Slack-compatible format
        "text": f"{emoji} *[{severity.upper()}]* Docker Sentinel Alert on {host_name or 'server'}\n"
                f"Score: {score:.2f} | Policy: {alert.get('policy_name', 'N/A')}\n"
                f"{alert.get('message', '')}",
        # Discord-compatible format
        "embeds": [{
            "title": f"{emoji} Docker Sentinel Alert",
            "description": alert.get("message", "Security alert triggered"),
            "color": {"low": 0x3FB950, "medium": 0xD29922, "high": 0xFF6600, "critical": 0xDA3633}.get(severity, 0x58A6FF),
            "fields": [
                {"name": "Severity", "value": severity.upper(), "inline": True},
                {"name": "Score", "value": f"{score:.2f}", "inline": True},
                {"name": "Policy", "value": alert.get("policy_name", "N/A"), "inline": True},
                {"name": "Host", "value": host_name or "N/A", "inline": True},
            ],
            "timestamp": alert.get("timestamp", ""),
        }],
    }


def _classify_event_type(alert: dict) -> str:
    """Classify alert into event type for webhook filtering."""
    severity = alert.get("severity", "")
    score = float(alert.get("score", 0))
    action = alert.get("action", "")

    if severity == "critical" or score >= 0.9:
        return "critical_alert"
    if action != "alert_only":
        return "action_taken"
    if score >= 0.8:
        return "attack_detected"
    return "anomaly_detected"
