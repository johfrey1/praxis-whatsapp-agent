"""Pagos por transferencia: llave Bre-B (recomendada), Nequi o Daviplata.

Flujo:
1. El agente pide los datos y llama `start_payment`: se crea un `ManualPayment` en estado
   `awaiting_proof` y el estudiante recibe adónde pagar.
2. El estudiante paga desde su app y envía la captura por WhatsApp. `receive_proof` la descarga,
   la guarda como evidencia, deja el pago en `pending_approval`, le envía un recibo "POR CONFIRMAR"
   y reenvía la captura al gestor de estudiantes.
3. Una persona aprueba o rechaza (`review`); el estudiante recibe el resultado y, si se aprueba, el
   recibo "CONFIRMADO" (que también va al número de servicio).
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
from app.services.receipt_image import render_manual_receipt
from app.whatsapp.client import WhatsAppClient

settings = get_settings()
logger = get_logger(__name__)

AWAITING_PROOF = "awaiting_proof"
PENDING_APPROVAL = "pending_approval"
APPROVED = "approved"
REJECTED = "rejected"

METHODS = {"breb": "Bre-B", "nequi": "Nequi", "daviplata": "Daviplata"}
CONCEPTS = {"cuota": "Cuota", **payment_service.SERVICE_CONCEPTS}
# Cuánto tiempo se espera la captura antes de dejar de asociar imágenes a este pago.
PROOF_WINDOW = timedelta(days=3)
_PROOF_MIME_TYPES = {"image/jpeg", "image/png", "image/webp", "application/pdf"}


class ManualPaymentError(ValueError):
    pass


def destination(method: str) -> str:
    """Adónde debe pagar el estudiante según el medio elegido."""
    if method == "nequi":
        return f"Nequi: {settings.nequi_key}"
    if method == "daviplata":
        return f"Daviplata: {settings.daviplata_number}"
    return f"Llave Bre-B: {settings.breb_key}\nA nombre de: {settings.breb_account_name}"


def instructions(payment: ManualPayment) -> str:
    amount = f"Valor: {payment_service.format_cop(payment.amount_in_cents)}\n" if payment.amount_in_cents else ""
    return f"{destination(payment.method)}\n{amount}Referencia: {payment.reference}"


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
    method: str = "breb",
) -> ManualPayment:
    if method not in METHODS:
        raise ManualPaymentError(f"Medio de pago no válido. Opciones: {', '.join(METHODS)}.")
    if concept not in CONCEPTS:
        raise ManualPaymentError(f"Trámite no válido. Opciones: {', '.join(CONCEPTS)}.")
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
        raise ManualPaymentError(str(exc)) from exc

    # Un pago a la espera de captura con los mismos datos se reutiliza (cambiando el medio si hace falta).
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
        existing.method = method
        if amount_in_cents:
            existing.amount_in_cents = amount_in_cents
        return existing

    payment = ManualPayment(
        contact_id=contact.id,
        conversation_id=conversation.id if conversation else None,
        reference=f"PRX-B{secrets.token_hex(5).upper()}",
        concept=concept,
        method=method,
        national_id=national_id,
        contract_number=contract_number,
        account_number=account_number or None,
        student_name=student_name or None,
        amount_in_cents=amount_in_cents,
        status=AWAITING_PROOF,
    )
    session.add(payment)
    await session.flush()
    logger.info("manual_payment_started", reference=payment.reference, concept=concept, method=method, wa_id=contact.wa_id)
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
    lines = [
        f"{status_line} ({METHODS.get(payment.method, payment.method)})",
        f"Trámite: {CONCEPTS.get(payment.concept, payment.concept)}",
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


def _save_receipt(payment: ManualPayment, png: bytes) -> None:
    receipts_dir = Path(settings.document_storage_path) / "receipts"
    receipts_dir.mkdir(parents=True, exist_ok=True)
    (receipts_dir / f"{payment.reference}.png").write_bytes(png)


async def _send_receipt_to_student(
    whatsapp_client: WhatsAppClient, payment: ManualPayment, contact: Contact, png: bytes, caption: str
) -> None:
    try:
        media_id = await whatsapp_client.upload_media(png, f"recibo-{payment.reference}.png", "image/png")
        await whatsapp_client.send_image_by_media_id(to=contact.wa_id, media_id=media_id, caption=caption)
    except Exception:
        logger.exception("manual_receipt_student_send_failed", reference=payment.reference)


async def receive_proof(
    session: AsyncSession,
    whatsapp_client: WhatsAppClient,
    payment: ManualPayment,
    contact: Contact,
    media_id: str,
) -> None:
    """Guarda la captura como evidencia, deja el pago en espera de aprobación, envía el recibo POR
    CONFIRMAR al estudiante y reenvía la captura al gestor."""
    data, mime_type = await whatsapp_client.download_media(media_id)
    mime_type = mime_type.split(";")[0].strip()
    if mime_type not in _PROOF_MIME_TYPES:
        raise ManualPaymentError("unsupported_proof_type")

    proofs_dir = Path(settings.document_storage_path) / "proofs"
    proofs_dir.mkdir(parents=True, exist_ok=True)
    path = proofs_dir / f"{payment.reference}{_extension(mime_type)}"
    path.write_bytes(data)

    payment.proof_media_id = media_id
    payment.proof_path = str(path)
    payment.proof_received_at = datetime.now(timezone.utc)
    payment.status = PENDING_APPROVAL
    await session.flush()
    logger.info("manual_payment_proof_received", reference=payment.reference, wa_id=contact.wa_id)

    try:
        png = render_manual_receipt(payment, contact, confirmed=False)
        _save_receipt(payment, png)
        await _send_receipt_to_student(
            whatsapp_client, payment, contact, png, f"Recibo {payment.reference}: por confirmar"
        )
    except Exception:
        logger.exception("manual_receipt_render_failed", reference=payment.reference)

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
            data, f"comprobante-{payment.reference}{_extension(mime_type)}", mime_type
        )
    except Exception:
        logger.exception("manual_proof_upload_failed", reference=payment.reference)
        media_id = None

    body_params = [
        METHODS.get(payment.method, payment.method),
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
            logger.exception("manual_notice_failed", reference=payment.reference, to=number)
        if media_id is None or not mime_type.startswith("image/"):
            continue
        try:
            await whatsapp_client.send_template(
                to=number,
                name=settings.manual_receipt_template_name,
                language=settings.manual_receipt_template_language,
                body_params=body_params,
                header_image_media_id=media_id,
            )
        except Exception:
            logger.warning("manual_template_failed_falling_back_to_image", reference=payment.reference, to=number)
            try:
                await whatsapp_client.send_image_by_media_id(to=number, media_id=media_id, caption=payment.reference)
            except Exception:
                logger.exception("manual_proof_forward_failed", reference=payment.reference, to=number)


async def _send_confirmed_receipt_to_service(
    whatsapp_client: WhatsAppClient, payment: ManualPayment, png: bytes
) -> None:
    """El recibo confirmado también va a los números de servicio, igual que los pagos por Wompi."""
    if not settings.receipt_numbers:
        return
    try:
        media_id = await whatsapp_client.upload_media(png, f"recibo-{payment.reference}.png", "image/png")
    except Exception:
        logger.exception("manual_receipt_upload_failed", reference=payment.reference)
        return
    body_params = [
        payment_service.format_cop(payment.amount_in_cents),
        payment.contract_number,
        payment.national_id,
        payment.reference,
    ]
    for number in settings.receipt_numbers:
        try:
            await whatsapp_client.send_template(
                to=number,
                name=settings.receipt_template_name,
                language=settings.receipt_template_language,
                body_params=body_params,
                header_image_media_id=media_id,
            )
        except Exception:
            try:
                await whatsapp_client.send_image_by_media_id(to=number, media_id=media_id, caption=payment.reference)
            except Exception:
                logger.exception("manual_receipt_service_send_failed", reference=payment.reference, to=number)


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
        raise ManualPaymentError(f"El pago está en estado {payment.status}; solo se revisan los pendientes de aprobación.")
    payment.status = APPROVED if approve else REJECTED
    payment.reviewed_at = datetime.now(timezone.utc)
    payment.review_note = (note or "").strip() or None
    await session.flush()
    logger.info("manual_payment_reviewed", reference=payment.reference, approved=approve)

    label = CONCEPTS.get(payment.concept, payment.concept).lower()
    method = METHODS.get(payment.method, payment.method)
    if approve:
        text = f"Tu pago por {method} de {label} fue confirmado. Referencia {payment.reference}."
        if payment.concept in payment_service.SERVICE_CONCEPTS:
            text += " Tu solicitud quedó registrada y el gestor de estudiantes la va a procesar."
    else:
        text = f"No pudimos confirmar tu pago por {method} de {label} (referencia {payment.reference})."
        if payment.review_note:
            text += f" Motivo: {payment.review_note}."
        text += " Escríbeme y lo revisamos juntos."
    try:
        await whatsapp_client.send_text(to=contact.wa_id, body=text)
    except Exception:
        logger.exception("manual_review_notice_failed", reference=payment.reference)

    if approve:
        try:
            png = render_manual_receipt(payment, contact, confirmed=True)
            _save_receipt(payment, png)
            await _send_receipt_to_student(whatsapp_client, payment, contact, png, f"Recibo {payment.reference}: confirmado")
            await _send_confirmed_receipt_to_service(whatsapp_client, payment, png)
        except Exception:
            logger.exception("manual_receipt_render_failed", reference=payment.reference)
        if payment.concept in payment_service.SERVICE_CONCEPTS:
            for number in settings.student_manager_numbers_list:
                try:
                    await whatsapp_client.send_staff_message(
                        to=number, text=format_notification(payment, contact, status_line="Requerimiento pagado y confirmado")
                    )
                except Exception:
                    logger.exception("manual_requirement_send_failed", reference=payment.reference, to=number)
    return payment
