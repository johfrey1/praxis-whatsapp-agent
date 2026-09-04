import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Contact, Conversation, ConversationStatus, Message, MessageDirection, MessageType
from app.logging_config import get_logger
from app.whatsapp.client import WhatsAppClient

settings = get_settings()
logger = get_logger(__name__)


async def get_or_create_contact(session: AsyncSession, wa_id: str, profile_name: str | None) -> tuple[Contact, bool]:
    result = await session.execute(select(Contact).where(Contact.wa_id == wa_id))
    contact = result.scalar_one_or_none()

    if contact is not None:
        was_new = False
        if profile_name and contact.profile_name != profile_name:
            contact.profile_name = profile_name
        contact.is_new = False
        return contact, was_new

    contact = Contact(wa_id=wa_id, profile_name=profile_name, is_new=True)
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


async def notify_staff(whatsapp_client: WhatsAppClient, contact: Contact, reason: str) -> None:
    """Redirige/alerta a los asesores humanos cuando hay un contacto nuevo o una escalación."""
    if not settings.staff_numbers:
        logger.warning("no_staff_numbers_configured", reason=reason, wa_id=contact.wa_id)
        return

    name = contact.profile_name or contact.wa_id
    text = f"🔔 {reason}\nContacto: {name} ({contact.wa_id})"
    for staff_number in settings.staff_numbers:
        try:
            await whatsapp_client.send_text(to=staff_number, body=text)
        except Exception:
            logger.exception("staff_notification_failed", staff_number=staff_number)
