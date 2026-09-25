import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.base import get_session
from app.db.models import Contact, PaymentRequest, PaymentStatus
from app.security import require_admin_api_key

router = APIRouter(prefix="/admin/payments", tags=["admin-payments"], dependencies=[Depends(require_admin_api_key)])


def _serialize(payment: PaymentRequest, wa_id: str) -> dict:
    return {
        "id": str(payment.id),
        "reference": payment.reference,
        "wa_id": wa_id,
        "national_id": payment.national_id,
        "contract_number": payment.contract_number,
        "account_number": payment.account_number,
        "status": payment.status.value,
        "amount_in_cents": payment.amount_in_cents,
        "currency": payment.currency,
        "payment_method_type": payment.payment_method_type,
        "wompi_transaction_id": payment.wompi_transaction_id,
        "wompi_payment_link_id": payment.wompi_payment_link_id,
        "payment_url": payment.payment_url,
        "paid_at": payment.paid_at.isoformat() if payment.paid_at else None,
        "expires_at": payment.expires_at.isoformat(),
        "created_at": payment.created_at.isoformat(),
    }


@router.get("")
async def list_payments(
    payment_status: PaymentStatus | None = None,
    national_id: str | None = None,
    contract_number: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    stmt = select(PaymentRequest, Contact).join(Contact, PaymentRequest.contact_id == Contact.id)
    if payment_status:
        stmt = stmt.where(PaymentRequest.status == payment_status)
    if national_id:
        stmt = stmt.where(PaymentRequest.national_id == national_id)
    if contract_number:
        stmt = stmt.where(PaymentRequest.contract_number == contract_number.upper())
    result = await session.execute(stmt.order_by(PaymentRequest.created_at.desc()).limit(500))
    return [_serialize(payment, contact.wa_id) for payment, contact in result.all()]


@router.get("/{payment_id}")
async def get_payment(payment_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> dict:
    """Detalle con la evidencia completa: cada evento de Wompi recibido y la transacción verificada."""
    result = await session.execute(
        select(PaymentRequest).options(selectinload(PaymentRequest.events)).where(PaymentRequest.id == payment_id)
    )
    payment = result.scalar_one_or_none()
    if payment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Payment not found")
    contact = await session.get(Contact, payment.contact_id)
    return {
        **_serialize(payment, contact.wa_id if contact else ""),
        "events": [
            {
                "id": str(e.id),
                "event": e.event,
                "wompi_transaction_id": e.wompi_transaction_id,
                "transaction_status": e.transaction_status,
                "amount_in_cents": e.amount_in_cents,
                "environment": e.environment,
                "checksum": e.checksum,
                "event_timestamp": e.event_timestamp,
                "received_at": e.received_at.isoformat(),
                "payload": e.payload,
                "verified_transaction": e.verified_transaction,
            }
            for e in payment.events
        ],
    }
