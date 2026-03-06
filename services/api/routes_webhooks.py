"""Webhook management API — CRUD for webhook configurations.

Reads and writes to /config/webhooks.yml for dynamic webhook management.
"""

from __future__ import annotations

import ipaddress
import os
import uuid
from typing import Any, Dict, List
from urllib.parse import urlparse

import structlog
import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

log = structlog.get_logger()

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

WEBHOOKS_CONFIG = os.environ.get("SENTINEL_CONFIG", "/config/sentinel.yml")
WEBHOOKS_FILE = "/config/webhooks.yml"

# SSRF protection — block internal/private IP ranges
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),  # link-local / cloud metadata
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),  # IPv6 private
    ipaddress.ip_network("fe80::/10"),  # IPv6 link-local
]


def _validate_webhook_url(url: str) -> None:
    """Validate webhook URL to prevent SSRF attacks.

    Blocks: private IPs, Docker internal ranges, cloud metadata endpoints,
    non-HTTP(S) schemes, and raw IP URLs in blocked ranges.
    """
    parsed = urlparse(url)

    # Only allow http and https schemes
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail=f"Invalid URL scheme: {parsed.scheme}. Only http/https allowed.")

    hostname = parsed.hostname
    if not hostname:
        raise HTTPException(status_code=400, detail="Invalid URL: no hostname")

    # Resolve hostname to check for private IPs
    import socket
    try:
        addr_info = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        for _, _, _, _, sockaddr in addr_info:
            ip = ipaddress.ip_address(sockaddr[0])
            for network in _BLOCKED_NETWORKS:
                if ip in network:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Webhook URL resolves to blocked address range ({network}). External URLs only."
                    )
    except socket.gaierror:
        raise HTTPException(status_code=400, detail=f"Cannot resolve hostname: {hostname}")
    except HTTPException:
        raise
    except Exception:
        pass  # Allow through if resolution fails for other reasons


class WebhookCreate(BaseModel):
    name: str
    url: str
    events: List[str] = ["attack_detected", "critical_alert"]
    enabled: bool = True
    sign_payloads: bool = False
    headers: Dict[str, str] = {}


class WebhookUpdate(BaseModel):
    name: str | None = None
    url: str | None = None
    events: List[str] | None = None
    enabled: bool | None = None
    sign_payloads: bool | None = None
    headers: Dict[str, str] | None = None


def _read_config() -> Dict[str, Any]:
    """Read webhooks.yml config."""
    try:
        with open(WEBHOOKS_FILE, "r") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {"version": "v0.2", "webhook_secret": "", "webhooks": []}
    except Exception as e:
        log.error("webhook_config_read_error", error=str(e))
        return {"version": "v0.2", "webhook_secret": "", "webhooks": []}


def _write_config(config: Dict[str, Any]) -> None:
    """Write webhooks.yml config."""
    try:
        with open(WEBHOOKS_FILE, "w") as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)
    except Exception as e:
        log.error("webhook_config_write_error", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to save webhook config")


@router.get("")
async def list_webhooks():
    """List all configured webhooks."""
    config = _read_config()
    webhooks = config.get("webhooks", [])
    # Add an ID if not present
    for i, wh in enumerate(webhooks):
        if "id" not in wh:
            wh["id"] = str(i)
    return {
        "webhooks": webhooks,
        "total": len(webhooks),
        "event_types": [
            "attack_detected",
            "anomaly_detected",
            "action_taken",
            "critical_alert",
        ],
    }


@router.post("")
async def create_webhook(webhook: WebhookCreate):
    """Create a new webhook configuration."""
    config = _read_config()
    webhooks = config.get("webhooks", [])

    # Check for duplicate name
    for wh in webhooks:
        if wh.get("name") == webhook.name:
            raise HTTPException(status_code=409, detail=f"Webhook '{webhook.name}' already exists")

    # SSRF protection: validate URL
    _validate_webhook_url(webhook.url)

    new_wh = {
        "id": str(uuid.uuid4())[:8],
        "name": webhook.name,
        "url": webhook.url,
        "events": webhook.events,
        "enabled": webhook.enabled,
        "sign_payloads": webhook.sign_payloads,
    }
    if webhook.headers:
        new_wh["headers"] = webhook.headers

    webhooks.append(new_wh)
    config["webhooks"] = webhooks
    _write_config(config)

    return {"status": "created", "webhook": new_wh}


@router.put("/{webhook_name}")
async def update_webhook(webhook_name: str, update: WebhookUpdate):
    """Update an existing webhook configuration."""
    config = _read_config()
    webhooks = config.get("webhooks", [])

    for wh in webhooks:
        if wh.get("name") == webhook_name:
            if update.url is not None:
                # SSRF protection: validate URL
                _validate_webhook_url(update.url)
            if update.name is not None:
                wh["name"] = update.name
            if update.url is not None:
                wh["url"] = update.url
            if update.events is not None:
                wh["events"] = update.events
            if update.enabled is not None:
                wh["enabled"] = update.enabled
            if update.sign_payloads is not None:
                wh["sign_payloads"] = update.sign_payloads
            if update.headers is not None:
                wh["headers"] = update.headers

            config["webhooks"] = webhooks
            _write_config(config)
            return {"status": "updated", "webhook": wh}

    raise HTTPException(status_code=404, detail=f"Webhook '{webhook_name}' not found")


@router.delete("/{webhook_name}")
async def delete_webhook(webhook_name: str):
    """Delete a webhook configuration."""
    config = _read_config()
    webhooks = config.get("webhooks", [])

    new_webhooks = [wh for wh in webhooks if wh.get("name") != webhook_name]
    if len(new_webhooks) == len(webhooks):
        raise HTTPException(status_code=404, detail=f"Webhook '{webhook_name}' not found")

    config["webhooks"] = new_webhooks
    _write_config(config)

    return {"status": "deleted", "name": webhook_name}


@router.post("/{webhook_name}/test")
async def test_webhook(webhook_name: str):
    """Send a test payload to a webhook."""
    config = _read_config()
    webhooks = config.get("webhooks", [])

    target = None
    for wh in webhooks:
        if wh.get("name") == webhook_name:
            target = wh
            break

    if not target:
        raise HTTPException(status_code=404, detail=f"Webhook '{webhook_name}' not found")

    # SSRF protection: re-validate URL before making request
    _validate_webhook_url(target["url"])

    # Import dispatcher
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            test_payload = {
                "event": "test",
                "message": "Docker Sentinel test webhook",
                "timestamp": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat().replace("+00:00", "Z"),
                "text": "Docker Sentinel: Test webhook delivery",
            }
            import json
            resp = await client.post(
                target["url"],
                content=json.dumps(test_payload),
                headers={"Content-Type": "application/json"},
            )
            return {
                "status": "sent",
                "http_status": resp.status_code,
                "response": resp.text[:200],
            }
    except Exception as e:
        return {"status": "error", "message": str(e)}
