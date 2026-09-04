import hashlib
import hmac

from fastapi import Header, HTTPException, status

from app.config import get_settings

settings = get_settings()


def verify_webhook_signature(raw_body: bytes, signature_header: str | None) -> None:
    """Valida X-Hub-Signature-256 enviado por Meta para confirmar que el payload
    proviene realmente de WhatsApp y no fue falsificado."""
    if not signature_header or not signature_header.startswith("sha256="):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing signature")

    expected = hmac.new(settings.whatsapp_app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    provided = signature_header.removeprefix("sha256=")

    if not hmac.compare_digest(expected, provided):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid signature")


async def require_admin_api_key(x_api_key: str = Header(...)) -> None:
    if not hmac.compare_digest(x_api_key, settings.admin_api_key):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid API key")
