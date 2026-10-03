"""Pago por llave Bre-B (opción recomendada junto a Wompi).

Flujo:
1. El agente pide los datos y llama `start_payment`: se crea un `ManualPayment` en estado
   `awaiting_proof` y el estudiante recibe la llave a la que debe pagar.
2. El estudiante paga desde su app bancaria y envía la captura por WhatsApp. `receive_proof` la
   descarga, la guarda como evidencia, deja el pago en `pending_approval` y la reenvía al gestor.
3. Una persona aprueba o rechaza (`review`); el estudiante recibe el resultado por WhatsApp.
"""

import mimetypes
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Contact, ManualPayment
from app.logging_config import get_logger
from app.services import payment_service
from app.whatsapp.client import WhatsAppClient

settings = get_settings()
logger = get_logger(__name__)

AWAITING_PROOF = "awaiting_proof"
PENDING_APPROVAL = "pending_approval"
APPROVED = "approved"
REJECTED = "rejected"

CONCEPTS = {"cuota": "Cuota", **payment_service.SERVICE_CONCEPTS}
# Cuánto tiempo se espera la captura antes de dejar de asociar imágenes a este pago.
PROOF_WINDOW = timedelta(days=3)
_PROOF_MIME_TYPES = {"image/jpeg", "image/png", "image/webp", "application/pdf"}


class BrebValidationError(ValueError):
    pass


def instructions(payment: ManualPayment) -> str:
    amount = f"Valor: {payment_service.format_cop(payment.amount_in_cents)}\n" if payment.amount_in_cents else ""
    return (
        f"Llave Bre-B: {settings.breb_key}\n"
        f"A nombre de: {settings.breb_account_name}\n"
        f"{amount}"
        f"Referencia: {payment.reference}"
    )


async def start_payment(
    session: AsyncSession,
    contact: Contact,
    conversation,
    concept: str,
    national_id: str,
    contract_number: str,
    account_number: str = "",
    student_name: str = "",
    amount_cop: int | None = None,
) -> ManualPayment:
    if concept not in CONCEPTS:
        raise BrebValidationError(f"Trámite no válido. Opciones: {', '.join(CONCEPTS)}.")
    try:
        if concept == "cuota":
            national_id, contract_number, account_number = payment_service.normalize_payment_data(
                national_id, contract_number, account_number
            )
            student_name = re.sub(r"\s+", " ", (student_name or "").strip())
            amount_in_cents = amount_cop * 100 if amount_cop and amount_cop > 0 else None
        else:
            national_id, contract_number, student_name = payment_service.normalize_service_data(
                national_id, contract_number, student_name
            )
            account_number = ""
            amount_in_cents = settings.service_fee_cop * 100
    except payment_service.PaymentValidationError as exc:
        raise BrebValidationError(str(exc)) from exc

    # Un pago a la espera de captura con los mismos datos se reutiliza en vez de duplicarlo.
    result = await session.execute(
        select(ManualPayment)
        .where(
            ManualPayment.contact_id == contact.id,
            ManualPayment.status == AWAITING_PROOF,
            ManualPayment.concept == concept,
            ManualPayment.national_id == national_id,
            ManualPayment.contract_number == contract_number,
        )
        .order_by(ManualPayment.created_at.desc())
    )
    existing = result.scalars().first()
    if existing is not None:
        return existing

    payment = ManualPayment(
        contact_id=contact.id,
        conversation_id=conversation.id if conversation else None,
        reference=f"PRX-B{secrets.token_hex(5).upper()}",
        concept=concept,
        national_id=national_id,
        contract_number=contract_number,
        account_number=account_number or None,
        student_name=student_name or None,
        amount_in_cents=amount_in_cents,
        status=AWAITING_PROOF,
    )
    session.add(payment)
    await session.flush()
    logger.info("breb_payment_started", reference=payment.reference, concept=concept, wa_id=contact.wa_id)
    return payment


async def find_awaiting_proof(session: AsyncSession, contact: Contact) -> ManualPayment | None:
    result = await session.execute(
        select(ManualPayment)
        .where(
            ManualPayment.contact_id == contact.id,
            ManualPayment.status == AWAITING_PROOF,
            ManualPayment.created_at > datetime.now(timezone.utc) - PROOF_WINDOW,
        )
        .order_by(ManualPayment.created_at.desc())
    )
    return result.scalars().first()


def format_notification(payment: ManualPayment, contact: Contact, *, status_line: str) -> str:
    label = CONCEPTS.get(payment.concept, payment.concept)
    lines = [
        f"{status_line} (Bre-B)",
        f"Trámite: {label}",
        f"Estudiante: {payment.student_name or contact.profile_name or 'Sin nombre'}",
        f"Cédula: {payment.national_id}",
        f"Contrato: {payment.contract_number}",
    ]
    if payment.account_number:
        lines.append(f"Cuenta: {payment.account_number}")
    if payment.amount_in_cents:
        lines.append(f"Valor: {payment_service.format_cop(payment.amount_in_cents)}")
    lines.append(f"WhatsApp: +{contact.wa_id}")
    lines.append(f"Referencia: {payment.reference}")
    return "\n".join(lines)


def _extension(mime_type: str) -> str:
    return {"image/jpeg": ".jpg", "application/pdf": ".pdf"}.get(mime_type) or mimetypes.guess_extension(mime_type) or ".bin"


async def receive_proof(
    session: AsyncSession,
    whatsapp_client: WhatsAppClient,
    payment: ManualPayment,
    contact: Contact,
    media_id: str,
) -> None:
    """Guarda la captura como evidencia, deja el pago en espera de aprobación y avisa al gestor."""
    data, mime_type = await whatsapp_client.download_media(media_id)
    if mime_type.split(";")[0].strip() not in _PROOF_MIME_TYPES:
        raise BrebValidationError("unsupported_proof_type")

    proofs_dir = Path(settings.document_storage_path) / "proofs"
    proofs_dir.mkdir(parents=True, exist_ok=True)
    path = proofs_dir / f"{payment.reference}{_extension(mime_type.split(';')[0].strip())}"
    path.write_bytes(data)

    payment.proof_media_id = media_id
    payment.proof_path = str(path)
    payment.proof_received_at = datetime.now(timezone.utc)
    payment.status = PENDING_APPROVAL
    await session.flush()
    logger.info("breb_proof_received", reference=payment.reference, wa_id=contact.wa_id)

    await _forward_to_manager(whatsapp_client, payment, contact, data, mime_type)


async def _forward_to_manager(
    whatsapp_client: WhatsAppClient, payment: ManualPayment, contact: Contact, data: bytes, mime_type: str
) -> None:
    numbers = settings.student_manager_numbers_list
    if not numbers:
        logger.error("no_student_manager_numbers_configured", reference=payment.reference)
        return
    summary = format_notification(payment, contact, status_line="Pago en espera de aprobación")
    try:
        media_id = await whatsapp_client.upload_media(
            data, f"comprobante-{payment.reference}{_extension(mime_type.split(';')[0].strip())}", mime_type
        )
    except Exception:
        logger.exception("breb_proof_upload_failed", reference=payment.reference)
        media_id = None

    body_params = [
        payment.student_name or contact.profile_name or "Sin nombre",
        payment.national_id,
        payment.contract_number,
        CONCEPTS.get(payment.concept, payment.concept),
        payment.reference,
    ]
    for number in numbers:
        # El aviso de texto (plantilla de personal) llega siempre; la imagen es el soporte.
        try:
            await whatsapp_client.send_staff_message(to=number, text=summary)
        except Exception:
            logger.exception("breb_notice_failed", reference=payment.reference, to=number)
        if media_id is None or not mime_type.startswith("image/"):
            continue
        try:
            await whatsapp_client.send_template(
                to=number,
                name=settings.breb_receipt_template_name,
                language=settings.breb_receipt_template_language,
                body_params=body_params,
                header_image_media_id=media_id,
            )
        except Exception:
            logger.warning("breb_template_failed_falling_back_to_image", reference=payment.reference, to=number)
            try:
                await whatsapp_client.send_image_by_media_id(to=number, media_id=media_id, caption=payment.reference)
            except Exception:
                logger.exception("breb_proof_forward_failed", reference=payment.reference, to=number)


async def review(
    session: AsyncSession,
    whatsapp_client: WhatsAppClient,
    payment: ManualPayment,
    contact: Contact,
    *,
    approve: bool,
    note: str | None = None,
) -> ManualPayment:
    if payment.status != PENDING_APPROVAL:
        raise BrebValidationError(f"El pago está en estado {payment.status}; solo se revisan los pendientes de aprobación.")
    payment.status = APPROVED if approve else REJECTED
    payment.reviewed_at = datetime.now(timezone.utc)
    payment.review_note = (note or "").strip() or None
    await session.flush()
    logger.info("breb_payment_reviewed", reference=payment.reference, approved=approve)

    label = CONCEPTS.get(payment.concept, payment.concept).lower()
    if approve:
        text = f"Tu pago por Bre-B de {label} fue aprobado. Referencia {payment.reference}."
        if payment.concept in payment_service.SERVICE_CONCEPTS:
            text += " Tu solicitud quedó registrada y el gestor de estudiantes la va a procesar."
    else:
        text = f"No pudimos aprobar tu pago por Bre-B de {label} (referencia {payment.reference})."
        if payment.review_note:
            text += f" Motivo: {payment.review_note}."
        text += " Escríbeme y lo revisamos juntos."
    try:
        await whatsapp_client.send_text(to=contact.wa_id, body=text)
    except Exception:
        logger.exception("breb_review_notice_failed", reference=payment.reference)

    if approve and payment.concept in payment_service.SERVICE_CONCEPTS:
        for number in settings.student_manager_numbers_list:
            try:
                await whatsapp_client.send_staff_message(
                    to=number, text=format_notification(payment, contact, status_line="Requerimiento pagado y aprobado")
                )
            except Exception:
                logger.exception("breb_requirement_send_failed", reference=payment.reference, to=number)
    return payment
