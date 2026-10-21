import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import (
    Contact,
    Conversation,
    ConversationStatus,
    Message,
    MessageDirection,
    MessageType,
    contact_handle,
    has_phone,
)
from app.logging_config import get_logger
from app.whatsapp.client import WhatsAppClient

settings = get_settings()
logger = get_logger(__name__)


async def get_or_create_contact(
    session: AsyncSession,
    wa_id: str,
    profile_name: str | None,
    *,
    user_id: str | None = None,
    username: str | None = None,
) -> tuple[Contact, bool]:
    """Busca al contacto por BSUID y por dirección (celular o BSUID). Así se reconoce al mismo cliente
    aunque un día escriba con número visible y otro solo con su nombre de usuario de WhatsApp."""
    contact = None
    if user_id:
        result = await session.execute(select(Contact).where(Contact.user_id == user_id))
        contact = result.scalar_one_or_none()
    if contact is None:
        result = await session.execute(select(Contact).where(Contact.wa_id == wa_id))
        contact = result.scalar_one_or_none()

    if contact is not None:
        if profile_name and contact.profile_name != profile_name:
            contact.profile_name = profile_name
        if user_id and not contact.user_id:
            contact.user_id = user_id
        if username and contact.username != username:
            contact.username = username
        if wa_id.isdigit() and not has_phone(contact):
            # Lo conocíamos solo por BSUID y ahora Meta comparte el celular: es mejor dirección.
            taken = await session.execute(select(Contact.id).where(Contact.wa_id == wa_id))
            if taken.scalar_one_or_none() is None:
                contact.wa_id = wa_id
        contact.is_new = False
        contact.last_seen_at = datetime.now(timezone.utc)
        return contact, False

    contact = Contact(wa_id=wa_id, user_id=user_id, username=username, profile_name=profile_name, is_new=True)
    session.add(contact)
    await session.flush()
    return contact, True


async def get_or_create_active_conversation(session: AsyncSession, contact: Contact) -> Conversation:
    result = await session.execute(
        select(Conversation)
        .where(Conversation.contact_id == contact.id, Conversation.status == ConversationStatus.active)
        .order_by(Conversation.started_at.desc())
    )
    conversation = result.scalars().first()
    if conversation is not None:
        return conversation

    conversation = Conversation(contact_id=contact.id, status=ConversationStatus.active)
    session.add(conversation)
    await session.flush()
    return conversation


async def message_already_processed(session: AsyncSession, wa_message_id: str | None) -> bool:
    """Meta reintenta la entrega si tarda en recibir el 200; evita responder dos veces lo mismo."""
    if not wa_message_id:
        return False
    result = await session.execute(select(Message.id).where(Message.wa_message_id == wa_message_id).limit(1))
    return result.scalar_one_or_none() is not None


async def record_message(
    session: AsyncSession,
    conversation: Conversation,
    direction: MessageDirection,
    message_type: MessageType,
    content: str | None = None,
    wa_message_id: str | None = None,
    media_id: str | None = None,
    tool_calls: dict | None = None,
) -> Message:
    message = Message(
        conversation_id=conversation.id,
        wa_message_id=wa_message_id,
        direction=direction,
        message_type=message_type,
        content=content,
        media_id=media_id,
        tool_calls=tool_calls,
    )
    session.add(message)
    await session.flush()
    return message


async def get_recent_messages(session: AsyncSession, conversation: Conversation, limit: int = 20) -> list[Message]:
    result = await session.execute(
        select(Message)
        .where(Message.conversation_id == conversation.id)
        .order_by(Message.created_at.desc())
        .limit(limit)
    )
    return list(reversed(result.scalars().all()))


async def escalate_conversation(session: AsyncSession, conversation: Conversation, contact: Contact) -> None:
    conversation.status = ConversationStatus.escalated
    contact.needs_human = True
    await session.flush()


async def notify_staff(
    whatsapp_client: WhatsAppClient, contact: Contact, reason: str, *, student: bool = False
) -> None:
    """Alerta a los asesores humanos por un contacto nuevo, un prospecto o una escalación.

    Con `student=True` (estudiante ya matriculado) el aviso va solo al número de atención a
    estudiantes (`STUDENT_REQUEST_NUMBERS`); los prospectos van a `STAFF_NOTIFICATION_NUMBERS`."""
    numbers = settings.student_request_numbers_list if student else settings.staff_numbers
    if not numbers:
        logger.warning("no_notification_numbers_configured", reason=reason, wa_id=contact.wa_id, student=student)
        return

    name = contact.profile_name or contact_handle(contact)
    text = f"🔔 {reason}\nContacto: {name} ({contact_handle(contact)})"
    for staff_number in numbers:
        try:
            await whatsapp_client.send_staff_message(to=staff_number, text=text)
        except Exception:
            logger.exception("staff_notification_failed", staff_number=staff_number)


_STATUS_RANK = {"sent": 1, "delivered": 2, "read": 3, "failed": 4}


async def update_delivery_statuses(session: AsyncSession, statuses: list) -> int:
    """Guarda en cada mensaje saliente si llegó, se leyó o falló. Meta puede enviar los estados
    en desorden: nunca se retrocede de 'read' a 'delivered'."""
    updated = 0
    for st in statuses:
        if not st.wa_message_id or st.status not in _STATUS_RANK:
            continue
        result = await session.execute(select(Message).where(Message.wa_message_id == st.wa_message_id))
        message = result.scalar_one_or_none()
        if message is None:
            continue
        if _STATUS_RANK[st.status] >= _STATUS_RANK.get(message.delivery_status or "", 0):
            message.delivery_status = st.status
        if st.status == "failed":
            message.delivery_error = " | ".join(
                str(part) for part in (st.error_code, st.error_title, st.error_details) if part
            )
        updated += 1
    await session.flush()
    return updated
