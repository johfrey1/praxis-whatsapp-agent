import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.base import get_session
from app.db.models import Contact, ManualPayment
from app.security import require_admin_api_key
from app.services import manual_payment_service
from app.whatsapp.client import WhatsAppClient

router = APIRouter(
    prefix="/admin/manual-payments", tags=["admin-manual-payments"], dependencies=[Depends(require_admin_api_key)]
)
settings = get_settings()


class ReviewIn(BaseModel):
    note: str | None = Field(default=None, max_length=500, description="Motivo (se envía al estudiante si se rechaza)")


def _serialize(payment: ManualPayment, wa_id: str) -> dict:
    return {
        "id": str(payment.id),
        "reference": payment.reference,
        "wa_id": wa_id,
        "concept": payment.concept,
        "method": payment.method,
        "student_name": payment.student_name,
        "national_id": payment.national_id,
        "contract_number": payment.contract_number,
        "account_number": payment.account_number,
        "amount_in_cents": payment.amount_in_cents,
        "status": payment.status,
        "has_proof": bool(payment.proof_path),
        "proof_received_at": payment.proof_received_at.isoformat() if payment.proof_received_at else None,
        "reviewed_at": payment.reviewed_at.isoformat() if payment.reviewed_at else None,
        "review_note": payment.review_note,
        "created_at": payment.created_at.isoformat(),
    }


async def _get_or_404(session: AsyncSession, payment_id: uuid.UUID) -> tuple[ManualPayment, Contact]:
    payment = await session.get(ManualPayment, payment_id)
    contact = await session.get(Contact, payment.contact_id) if payment else None
    if payment is None or contact is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Payment not found")
    return payment, contact


@router.get("")
async def list_manual_payments(
    payment_status: str | None = None, session: AsyncSession = Depends(get_session)
) -> list[dict]:
    stmt = select(ManualPayment, Contact).join(Contact, ManualPayment.contact_id == Contact.id)
    if payment_status:
        stmt = stmt.where(ManualPayment.status == payment_status)
    result = await session.execute(stmt.order_by(ManualPayment.created_at.desc()).limit(500))
    return [_serialize(payment, contact.wa_id) for payment, contact in result.all()]


@router.get("/{payment_id}")
async def get_manual_payment(payment_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> dict:
    payment, contact = await _get_or_404(session, payment_id)
    return _serialize(payment, contact.wa_id)


@router.get("/{payment_id}/proof")
async def get_proof(payment_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> FileResponse:
    """La captura que envió el estudiante (evidencia del pago)."""
    payment, _ = await _get_or_404(session, payment_id)
    path = Path(payment.proof_path) if payment.proof_path else None
    if path is None or not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Proof not available")
    return FileResponse(path)


async def _review(payment_id: uuid.UUID, body: ReviewIn, session: AsyncSession, approve: bool) -> dict:
    payment, contact = await _get_or_404(session, payment_id)
    try:
        await manual_payment_service.review(session, WhatsAppClient(), payment, contact, approve=approve, note=body.note)
    except manual_payment_service.ManualPaymentError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    await session.commit()
    return _serialize(payment, contact.wa_id)


@router.post("/{payment_id}/approve")
async def approve_payment(
    payment_id: uuid.UUID, body: ReviewIn | None = None, session: AsyncSession = Depends(get_session)
) -> dict:
    return await _review(payment_id, body or ReviewIn(), session, approve=True)


@router.post("/{payment_id}/reject")
async def reject_payment(
    payment_id: uuid.UUID, body: ReviewIn | None = None, session: AsyncSession = Depends(get_session)
) -> dict:
    return await _review(payment_id, body or ReviewIn(), session, approve=False)
