from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.base import get_session
from app.logging_config import get_logger
from app.services import payment_service
from app.whatsapp.client import WhatsAppClient
from app.wompi.client import WompiClient

router = APIRouter(prefix="/payments/wompi", tags=["payments"])
settings = get_settings()
logger = get_logger(__name__)


@router.post("/events")
async def receive_wompi_event(request: Request, session: AsyncSession = Depends(get_session)) -> dict:
    """URL de eventos configurada en el dashboard de Wompi. Wompi reintenta si no recibe 200."""
    try:
        event = await request.json()
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid JSON")

    if not payment_service.verify_event_checksum(event, settings.wompi_events_secret):
        transaction = (event.get("data") or {}).get("transaction") or {}
        logger.warning(
            "wompi_invalid_checksum",
            wompi_event=event.get("event"),
            environment=event.get("environment"),
            transaction_id=transaction.get("id"),
            transaction_status=transaction.get("status"),
            payment_link_id=transaction.get("payment_link_id"),
            properties=(event.get("signature") or {}).get("properties"),
            secret_environment=settings.wompi_events_secret.split("_", 1)[0] or "missing",
        )
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid checksum")

    try:
        payment = await payment_service.process_wompi_event(session, WompiClient(), event)
        await session.commit()
    except Exception:
        await session.rollback()
        logger.exception("wompi_event_processing_failed")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Processing failed")

    # La notificación va después del commit: si falla, la evidencia y el estado ya quedaron guardados.
    if payment is not None:
        try:
            await payment_service.notify_payment_result(session, WhatsAppClient(), payment)
            await session.commit()
        except Exception:
            await session.rollback()
            logger.exception("payment_notification_failed", reference=payment.reference)

    return {"status": "ok"}
