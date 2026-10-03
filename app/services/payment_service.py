"""Pago de cuotas con Wompi (links de pago de monto abierto).

Flujo:
1. El agente pide cédula, número de contrato y número de cuenta y llama a
   `create_installment_payment`, que crea un link de Wompi de un solo uso SIN monto: el
   estudiante escribe el valor de la cuota en el checkout de Wompi.
2. Wompi notifica el resultado a POST /payments/wompi/events. Se valida el checksum, se consulta
   la transacción en la API de Wompi y se guarda todo como evidencia (`PaymentEvent`).
3. Si el estado final cambió, se confirma al estudiante por WhatsApp y se avisa a los asesores.
"""

import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import (
    Contact,
    Conversation,
    MessageDirection,
    MessageType,
    PaymentEvent,
    PaymentRequest,
    PaymentStatus,
)
from app.logging_config import get_logger
from app.services import conversation_service
from app.services.receipt_image import render_payment_receipt
from app.whatsapp.client import WhatsAppClient
from app.wompi.client import WompiClient, checkout_url

settings = get_settings()
logger = get_logger(__name__)

_NATIONAL_ID_RE = re.compile(r"^\d{5,12}$")
_CODE_RE = re.compile(r"^[A-Z0-9-]{1,40}$")

_WOMPI_STATUS_MAP = {
    "APPROVED": PaymentStatus.approved,
    "DECLINED": PaymentStatus.declined,
    "VOIDED": PaymentStatus.voided,
    "ERROR": PaymentStatus.error,
}


_STATUS_LABELS = {
    PaymentStatus.declined: "rechazado",
    PaymentStatus.voided: "anulado",
    PaymentStatus.error: "error en la transacción",
}


CONCEPT_CUOTA = "cuota"
# Trámites que el estudiante antiguo paga antes de que se genere el requerimiento. Sábana de notas
# y paz y salvo no tienen costo y van por submit_student_request.
SERVICE_CONCEPTS = {
    "certificado": "Certificado",
}


class PaymentValidationError(ValueError):
    pass


def normalize_service_data(national_id: str, contract_number: str, student_name: str) -> tuple[str, str, str]:
    """Valida cédula, contrato y nombres y apellidos del estudiante para un trámite con costo."""
    national_id, contract_number, _ = normalize_payment_data(national_id, contract_number, "N-A")
    student_name = re.sub(r"\s+", " ", (student_name or "").strip())
    words = student_name.split(" ") if student_name else []
    if len(words) < 2 or len(student_name) > 120 or not all(re.fullmatch(r"[^\W\d_]+(?:['.-][^\W\d_]+)*\.?", w) for w in words):
        raise PaymentValidationError("Se necesitan los nombres y apellidos completos del estudiante, solo letras.")
    return national_id, contract_number, student_name


def normalize_payment_data(national_id: str, contract_number: str, account_number: str) -> tuple[str, str, str]:
    """Limpia puntos/espacios/guiones que la gente suele escribir y valida el formato."""
    national_id = re.sub(r"[\s.\-]", "", national_id or "")
    contract_number = re.sub(r"\s", "", contract_number or "").upper()
    account_number = re.sub(r"\s", "", account_number or "").upper()

    if not _NATIONAL_ID_RE.match(national_id):
        raise PaymentValidationError("La cédula debe tener solo números (entre 5 y 12 dígitos).")
    if not _CODE_RE.match(contract_number):
        raise PaymentValidationError("El número de contrato solo puede tener letras, números y guiones.")
    if not _CODE_RE.match(account_number):
        raise PaymentValidationError("El número de cuenta solo puede tener letras, números y guiones.")
    return national_id, contract_number, account_number


def format_cop(amount_in_cents: int | None) -> str:
    if amount_in_cents is None:
        return "—"
    return "$" + f"{amount_in_cents / 100:,.0f}".replace(",", ".") + " COP"


async def create_installment_payment(
    session: AsyncSession,
    wompi_client: WompiClient,
    contact: Contact,
    conversation: Conversation | None,
    national_id: str,
    contract_number: str,
    account_number: str,
) -> PaymentRequest:
    national_id, contract_number, account_number = normalize_payment_data(national_id, contract_number, account_number)
    now = datetime.now(timezone.utc)

    # Si ya hay un link vigente con los mismos datos, se reutiliza en vez de crear otro.
    result = await session.execute(
        select(PaymentRequest)
        .where(
            PaymentRequest.contact_id == contact.id,
            PaymentRequest.status == PaymentStatus.pending,
            PaymentRequest.concept == CONCEPT_CUOTA,
            PaymentRequest.national_id == national_id,
            PaymentRequest.contract_number == contract_number,
            PaymentRequest.account_number == account_number,
            PaymentRequest.expires_at > now + timedelta(minutes=30),
        )
        .order_by(PaymentRequest.created_at.desc())
    )
    existing = result.scalars().first()
    if existing is not None:
        return existing

    reference = f"PRX-{secrets.token_hex(6).upper()}"
    expires_at = now + timedelta(hours=settings.wompi_payment_link_ttl_hours)
    link = await wompi_client.create_payment_link(
        name=f"Cuota Praxis - Contrato {contract_number}",
        description=f"Pago de cuota. Contrato {contract_number}, cuenta {account_number}. Ref {reference}",
        sku=reference,
        expires_at=expires_at,
    )

    payment = PaymentRequest(
        contact_id=contact.id,
        conversation_id=conversation.id if conversation else None,
        reference=reference,
        national_id=national_id,
        contract_number=contract_number,
        account_number=account_number,
        wompi_payment_link_id=link["id"],
        payment_url=checkout_url(link["id"]),
        status=PaymentStatus.pending,
        expires_at=expires_at,
    )
    session.add(payment)
    await session.flush()
    logger.info("payment_link_created", reference=reference, wa_id=contact.wa_id)
    return payment


async def create_service_payment(
    session: AsyncSession,
    wompi_client: WompiClient,
    contact: Contact,
    conversation: Conversation | None,
    concept: str,
    national_id: str,
    contract_number: str,
    student_name: str,
) -> PaymentRequest:
    """Link de Wompi de valor fijo (`service_fee_cop`) para un trámite con costo (certificado). El requerimiento se envía al gestor cuando Wompi confirma el pago."""
    if concept not in SERVICE_CONCEPTS:
        raise PaymentValidationError(f"Trámite no válido. Opciones: {', '.join(SERVICE_CONCEPTS)}.")
    national_id, contract_number, student_name = normalize_service_data(national_id, contract_number, student_name)
    now = datetime.now(timezone.utc)

    result = await session.execute(
        select(PaymentRequest)
        .where(
            PaymentRequest.contact_id == contact.id,
            PaymentRequest.status == PaymentStatus.pending,
            PaymentRequest.concept == concept,
            PaymentRequest.national_id == national_id,
            PaymentRequest.contract_number == contract_number,
            PaymentRequest.expires_at > now + timedelta(minutes=30),
        )
        .order_by(PaymentRequest.created_at.desc())
    )
    existing = result.scalars().first()
    if existing is not None:
        return existing

    reference = f"PRX-{secrets.token_hex(6).upper()}"
    expires_at = now + timedelta(hours=settings.wompi_payment_link_ttl_hours)
    label = SERVICE_CONCEPTS[concept]
    link = await wompi_client.create_payment_link(
        name=f"{label} Praxis - Contrato {contract_number}",
        description=f"{label}. Contrato {contract_number}, cédula {national_id}, {student_name}. Ref {reference}",
        sku=reference,
        expires_at=expires_at,
        amount_in_cents=settings.service_fee_cop * 100,
    )
    payment = PaymentRequest(
        contact_id=contact.id,
        conversation_id=conversation.id if conversation else None,
        reference=reference,
        national_id=national_id,
        contract_number=contract_number,
        account_number=None,
        concept=concept,
        student_name=student_name,
        wompi_payment_link_id=link["id"],
        payment_url=checkout_url(link["id"]),
        status=PaymentStatus.pending,
        expires_at=expires_at,
    )
    session.add(payment)
    await session.flush()
    logger.info("service_payment_link_created", reference=reference, concept=concept, wa_id=contact.wa_id)
    return payment


async def send_installment_reminder(
    session: AsyncSession,
    whatsapp_client: WhatsAppClient,
    wompi_client: WompiClient,
    contact: Contact,
    student_name: str,
    national_id: str,
    contract_number: str,
    account_number: str,
) -> PaymentRequest:
    """Cobro iniciado por la academia: crea el link y lo envía con la plantilla aprobada, que llega
    aunque el estudiante no haya escrito en las últimas 24 h."""
    conversation = await conversation_service.get_or_create_active_conversation(session, contact)
    payment = await create_installment_payment(
        session, wompi_client, contact, conversation, national_id, contract_number, account_number
    )
    # Se guarda antes de enviar: el link ya existe en Wompi y el webhook debe encontrar este pago.
    await session.commit()
    first_name = (student_name or "").strip().split(" ")[0] or "estudiante"
    await whatsapp_client.send_template(
        to=contact.wa_id,
        name=settings.payment_template_name,
        language=settings.payment_template_language,
        body_params=[first_name, payment.contract_number, payment.account_number],
        url_button_suffix=payment.wompi_payment_link_id,
    )
    await conversation_service.record_message(
        session,
        conversation,
        direction=MessageDirection.outbound,
        message_type=MessageType.interactive,
        content=(
            f"Recordatorio de cuota (plantilla {settings.payment_template_name}) contrato "
            f"{payment.contract_number}, cuenta {payment.account_number}\n[Botón Pagar cuota: {payment.payment_url}]"
        ),
    )
    await session.commit()
    logger.info("payment_reminder_sent", reference=payment.reference, wa_id=contact.wa_id)
    return payment


async def list_contact_payments(session: AsyncSession, contact: Contact, limit: int = 3) -> list[PaymentRequest]:
    result = await session.execute(
        select(PaymentRequest)
        .where(PaymentRequest.contact_id == contact.id)
        .order_by(PaymentRequest.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


def verify_event_checksum(event: dict, events_secret: str) -> bool:
    """Checksum de Wompi: SHA256(valores de signature.properties + timestamp + secreto de eventos)."""
    signature = event.get("signature") or {}
    checksum = str(signature.get("checksum") or "")
    if not events_secret or not checksum:
        return False

    data = event.get("data") or {}
    parts = []
    for prop in signature.get("properties") or []:
        value = data
        for key in str(prop).split("."):
            value = value.get(key) if isinstance(value, dict) else None
        parts.append("" if value is None else str(value))

    raw = "".join(parts) + str(event.get("timestamp", "")) + events_secret
    expected = hashlib.sha256(raw.encode()).hexdigest()
    return hmac.compare_digest(expected, checksum.lower())


async def process_wompi_event(session: AsyncSession, wompi_client: WompiClient, event: dict) -> PaymentRequest | None:
    """Guarda el evento como evidencia y actualiza la solicitud de pago.

    Devuelve la solicitud solo si su estado final cambió (hay que notificar al estudiante).
    Se asume que el checksum ya fue validado.
    """
    transaction = (event.get("data") or {}).get("transaction") or {}
    transaction_id = transaction.get("id")

    # Doble verificación: el estado se toma de la API de Wompi, no solo del evento.
    verified = None
    if event.get("event") == "transaction.updated" and transaction_id:
        try:
            verified = await wompi_client.get_transaction(transaction_id)
        except Exception:
            logger.warning("wompi_transaction_lookup_failed", transaction_id=transaction_id)
    source = verified or transaction

    payment = None
    payment_link_id = source.get("payment_link_id") or transaction.get("payment_link_id")
    if payment_link_id:
        result = await session.execute(
            select(PaymentRequest).where(PaymentRequest.wompi_payment_link_id == str(payment_link_id))
        )
        payment = result.scalar_one_or_none()

    session.add(
        PaymentEvent(
            payment_request_id=payment.id if payment else None,
            event=str(event.get("event") or "unknown"),
            wompi_transaction_id=transaction_id,
            transaction_status=source.get("status"),
            amount_in_cents=source.get("amount_in_cents"),
            environment=event.get("environment"),
            checksum=str((event.get("signature") or {}).get("checksum") or ""),
            event_timestamp=event.get("timestamp"),
            payload=event,
            verified_transaction=verified,
        )
    )
    await session.flush()

    if payment is None:
        logger.warning("wompi_event_without_payment_request", transaction_id=transaction_id, link_id=payment_link_id)
        return None

    new_status = _WOMPI_STATUS_MAP.get(str(source.get("status")))
    # PENDING no es estado final; un pago aprobado nunca se sobrescribe.
    if new_status is None or payment.status == PaymentStatus.approved or payment.status == new_status:
        return None

    payment.status = new_status
    payment.wompi_transaction_id = transaction_id
    payment.amount_in_cents = source.get("amount_in_cents")
    payment.currency = source.get("currency")
    payment.payment_method_type = source.get("payment_method_type")
    if new_status == PaymentStatus.approved:
        payment.paid_at = datetime.now(timezone.utc)
    await session.flush()
    logger.info("payment_status_changed", reference=payment.reference, status=new_status.value)
    return payment


async def send_payment_button(
    session: AsyncSession,
    whatsapp_client: WhatsAppClient,
    payment: PaymentRequest,
    to_wa_id: str,
    conversation: Conversation | None,
) -> None:
    """Envía el link de Wompi como botón: se abre en una ventana dentro de WhatsApp."""
    if payment.concept in SERVICE_CONCEPTS:
        header = f"Pago de {SERVICE_CONCEPTS[payment.concept].lower()}"[:60]
        body = (
            f"Contrato {payment.contract_number} · {payment.student_name}\n"
            f"Valor: {format_cop(settings.service_fee_cop * 100)}. Toca el botón y elige cómo pagar "
            "(Nequi, PSE, tarjeta, Bancolombia, Daviplata)."
        )
    else:
        header = "Pago de cuota"
        body = (
            f"Contrato {payment.contract_number} · Cuenta {payment.account_number}\n"
            "Toca el botón, escribe el valor de tu cuota y elige cómo pagar "
            "(Nequi, PSE, tarjeta, Bancolombia, Daviplata)."
        )
    footer = f"Ref {payment.reference} · válido hasta {_bogota(payment.expires_at).strftime('%d/%m %I:%M %p')}"
    await whatsapp_client.send_cta_url(
        to=to_wa_id,
        header=header,
        body=body,
        button_text="Pagar" if payment.concept in SERVICE_CONCEPTS else "Pagar cuota",
        url=payment.payment_url,
        footer=footer,
    )
    if conversation is not None:
        await conversation_service.record_message(
            session,
            conversation,
            direction=MessageDirection.outbound,
            message_type=MessageType.interactive,
            content=f"{body}\n[Botón Pagar cuota: {payment.payment_url}]",
        )


def _bogota(value: datetime) -> datetime:
    return value.astimezone(timezone(timedelta(hours=-5)))


def _link_still_valid(payment: PaymentRequest) -> bool:
    return payment.expires_at > datetime.now(timezone.utc)


def format_service_requirement(payment: PaymentRequest, contact: Contact) -> str:
    """Requerimiento ya pagado: lo recibe el gestor de estudiantes y, en copia, quien pagó."""
    paid_at = _bogota(payment.paid_at) if payment.paid_at else None
    return "\n".join(
        [
            f"Requerimiento de {SERVICE_CONCEPTS[payment.concept].lower()} (pagado)",
            f"Estudiante: {payment.student_name}",
            f"Cédula: {payment.national_id}",
            f"Contrato: {payment.contract_number}",
            f"WhatsApp: +{contact.wa_id}",
            f"Valor pagado: {format_cop(payment.amount_in_cents)} ({payment.payment_method_type or 'medio no informado'})",
            f"Fecha de pago: {paid_at.strftime('%d/%m/%Y %I:%M %p') if paid_at else '—'}",
            f"Referencia: {payment.reference} · Transacción Wompi: {payment.wompi_transaction_id}",
        ]
    )


def _result_message(payment: PaymentRequest, contact: Contact | None = None) -> str:
    if payment.concept in SERVICE_CONCEPTS:
        label = SERVICE_CONCEPTS[payment.concept].lower()
        if payment.status == PaymentStatus.approved and contact is not None:
            return (
                f"Recibimos tu pago. Tu solicitud de {label} quedó registrada y el gestor de estudiantes "
                "la va a procesar. Esta es la copia del requerimiento:\n\n"
                f"{format_service_requirement(payment, contact)}"
            )
        retry_hint = (
            "Puedes intentarlo de nuevo con el botón de abajo."
            if _link_still_valid(payment)
            else "Escríbeme y te genero un link nuevo."
        )
        return (
            f"Tu pago de {label} no fue aprobado "
            f"(estado: {_STATUS_LABELS.get(payment.status, payment.status.value)}). {retry_hint}"
        )
    if payment.status == PaymentStatus.approved:
        paid_at = _bogota(payment.paid_at) if payment.paid_at else None
        return (
            "Tu pago fue recibido.\n"
            f"Cuota del contrato {payment.contract_number} (cuenta {payment.account_number})\n"
            f"Valor: {format_cop(payment.amount_in_cents)}\n"
            f"Medio: {payment.payment_method_type or '—'}\n"
            f"Fecha: {paid_at.strftime('%d/%m/%Y %I:%M %p') if paid_at else '—'}\n"
            f"Transacción Wompi: {payment.wompi_transaction_id}\n"
            f"Referencia: {payment.reference}\n"
            "Guarda este mensaje como comprobante."
        )
    retry_hint = (
        "Puedes intentarlo de nuevo con el botón de abajo."
        if _link_still_valid(payment)
        else "Escribe *pagar cuota* para generar un link nuevo."
    )
    return (
        f"Tu pago de la cuota del contrato {payment.contract_number} no fue aprobado "
        f"(estado: {_STATUS_LABELS.get(payment.status, payment.status.value)}). {retry_hint}"
    )


async def notify_payment_result(
    session: AsyncSession, whatsapp_client: WhatsAppClient, payment: PaymentRequest
) -> None:
    contact = await session.get(Contact, payment.contact_id)
    if contact is None:
        return

    # Primero el gestor: si falla el mensaje al estudiante, el trámite pagado no se pierde.
    if payment.status == PaymentStatus.approved and payment.concept in SERVICE_CONCEPTS:
        await send_requirement_to_manager(whatsapp_client, payment, contact)

    text = _result_message(payment, contact)
    await whatsapp_client.send_text(to=contact.wa_id, body=text)

    conversation = await session.get(Conversation, payment.conversation_id) if payment.conversation_id else None
    if conversation is not None:
        await conversation_service.record_message(
            session,
            conversation,
            direction=MessageDirection.outbound,
            message_type=MessageType.system,
            content=text,
        )

    if payment.status != PaymentStatus.approved and _link_still_valid(payment):
        await send_payment_button(session, whatsapp_client, payment, contact.wa_id, conversation)

    if payment.status == PaymentStatus.approved and payment.concept not in SERVICE_CONCEPTS:
        try:
            await send_receipt_image(whatsapp_client, payment, contact)
        except Exception:
            logger.exception("payment_receipt_image_failed", reference=payment.reference)
        await conversation_service.notify_staff(
            whatsapp_client,
            contact,
            f"Pago de cuota APROBADO: {format_cop(payment.amount_in_cents)} · contrato {payment.contract_number} "
            f"· cuenta {payment.account_number} · cédula {payment.national_id} · tx {payment.wompi_transaction_id}",
            student=True,
        )


async def send_requirement_to_manager(
    whatsapp_client: WhatsAppClient, payment: PaymentRequest, contact: Contact
) -> None:
    """Envía el requerimiento pagado al gestor de estudiantes (el pagador ya recibió su copia)."""
    numbers = settings.student_manager_numbers_list
    if not numbers:
        logger.error("no_student_manager_numbers_configured", reference=payment.reference)
        return
    text = format_service_requirement(payment, contact)
    for number in numbers:
        try:
            await whatsapp_client.send_staff_message(to=number, text=text)
        except Exception:
            logger.exception("service_requirement_send_failed", reference=payment.reference, to=number)


async def send_receipt_image(whatsapp_client: WhatsAppClient, payment: PaymentRequest, contact: Contact) -> None:
    """Genera el comprobante como imagen, lo guarda como evidencia y lo envía al estudiante y al
    número de servicio de estudiantes. Al número de servicio va por plantilla (llega aunque no haya
    escrito en 24 h) y, si la plantilla falla, como imagen libre."""
    png = render_payment_receipt(payment, contact)

    receipts_dir = Path(settings.document_storage_path) / "receipts"
    receipts_dir.mkdir(parents=True, exist_ok=True)
    (receipts_dir / f"{payment.reference}.png").write_bytes(png)

    media_id = await whatsapp_client.upload_media(png, f"comprobante-{payment.reference}.png", "image/png")
    caption = (
        f"Comprobante pago de cuota {format_cop(payment.amount_in_cents)} · contrato {payment.contract_number} "
        f"· cédula {payment.national_id} · ref {payment.reference}"
    )
    # El estudiante acaba de pagar desde el chat: su ventana de 24 h está abierta.
    try:
        await whatsapp_client.send_image_by_media_id(to=contact.wa_id, media_id=media_id, caption=caption)
    except Exception:
        logger.exception("payment_receipt_student_send_failed", reference=payment.reference)

    if not settings.receipt_numbers:
        logger.warning("no_receipt_numbers_configured", reference=payment.reference)
        return
    body_params = [format_cop(payment.amount_in_cents), payment.contract_number, payment.national_id, payment.reference]
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
            logger.warning("payment_receipt_template_failed_falling_back_to_image", reference=payment.reference, to=number)
            try:
                await whatsapp_client.send_image_by_media_id(to=number, media_id=media_id, caption=caption)
            except Exception:
                logger.exception("payment_receipt_send_failed", reference=payment.reference, to=number)
