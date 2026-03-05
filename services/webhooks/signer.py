"""HMAC signing for webhook payloads."""
import hashlib
import hmac
import structlog

logger = structlog.get_logger("sentinel.webhooks.signer")


def sign_payload(payload: bytes, secret: str) -> str:
    """
    Generate HMAC-SHA256 signature for a webhook payload.
    
    Args:
        payload: Raw JSON body bytes
        secret: Webhook secret key
        
    Returns:
        Signature string in format "sha256=HEXDIGEST"
    """
    if not secret:
        logger.warning("webhook_signing_no_secret")
        return ""

    signature = hmac.new(
        secret.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()

    return f"sha256={signature}"


def verify_signature(payload: bytes, secret: str, signature: str) -> bool:
    """
    Verify a webhook payload signature.
    
    Args:
        payload: Raw body bytes
        secret: Webhook secret key
        signature: Signature to verify (sha256=...)
        
    Returns:
        True if signature is valid
    """
    if not secret:
        return False
    expected = sign_payload(payload, secret)
    return hmac.compare_digest(expected, signature)
