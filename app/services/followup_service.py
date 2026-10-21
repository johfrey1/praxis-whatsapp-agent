"""Conversaciones sin terminar: si el cliente deja de responder sin dejar sus datos, sin pagar y sin
que se le pase a una persona, el encargado recibe un aviso con lo que preguntó para retomarlo."""
import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Contact, Conversation, ConversationStatus, Message, MessageDirection
from app.logging_config import get_logger
from app.services import conversation_service
from app.whatsapp.client import WhatsAppClient

settings = get_settings()
logger = get_logger(__name__)

# Más allá de 24 h ya no es una conversación "en curso" (y la ventana de Meta para responderle se cerró).
LOOKBACK = timedelta(hours=24)
# Herramientas con las que la conversación sí terminó: el asesor ya recibió el aviso o el cliente ya tiene
# cómo pagar. Con cualquiera de ellas no hace falta el seguimiento.
_FINISHING_TOOLS = {
    "save_lead",
    "escalate_to_human",
    "submit_student_request",
    "create_installment_payment_link",
    "create_service_payment_link",
    "start_manual_payment",
}
# Herramientas que solo usan estudiantes ya matriculados: el aviso va a atención a estudiantes.
_STUDENT_TOOLS = {"get_payment_status"}


def _tools_used(messages: list[Message]) -> set[str]:
    used = set()
    for message in messages:
        for call in (message.tool_calls or {}).get("calls", []):
            result = call.get("result")
            # Una llamada que falló (p. ej. save_lead sin teléfono) no cierra la conversación.
            if call.get("tool") and not (isinstance(result, dict) and "error" in result):
                used.add(call["tool"])
    return used


def _shorten(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def needs_followup(messages: list[Message], now: datetime, idle: timedelta) -> bool:
    """`messages`: los de la conversación desde el último aviso (o las últimas 24 h), en orden."""
    if not any(m.direction == MessageDirection.inbound for m in messages):
        return False
    if now - messages[-1].created_at < idle:
        return False  # Sigue conversando.
    return not (_tools_used(messages) & _FINISHING_TOOLS)


def build_summary(messages: list[Message], idle: timedelta) -> str:
    asked = [m.content for m in messages if m.direction == MessageDirection.inbound and m.content]
    minutes = int(idle.total_seconds() // 60)
    lines = [f"Conversación sin terminar: dejó de responder hace más de {minutes} min, sin dejar datos. Retomarla 📞"]
    if asked:
        lines.append(f"💬 Escribió: {_shorten(' / '.join(asked), 350)}")
    else:
        lines.append("💬 Solo envió audio, imagen o archivo")
    last = messages[-1]
    if last.direction == MessageDirection.outbound and last.content:
        lines.append(f"🤖 Última respuesta: {_shorten(last.content, 200)}")
    elif last.direction == MessageDirection.inbound:
        lines.append("⚠️ Su último mensaje quedó sin respuesta del asistente")
    return "\n".join(lines)


def _internal_numbers() -> set[str]:
    """El equipo también escribe al número (p. ej. para aprobar pagos): esos chats no se siguen."""
    return {
        *settings.staff_numbers,
        *settings.student_request_numbers_list,
        *settings.student_manager_numbers_list,
        *settings.receipt_numbers,
    }


async def sweep(session: AsyncSession, whatsapp_client: WhatsAppClient, now: datetime | None = None) -> int:
    """Avisa al encargado por cada conversación abandonada. Devuelve cuántos avisos envió."""
    now = now or datetime.now(timezone.utc)
    idle = timedelta(minutes=settings.handoff_after_minutes)
    since = now - LOOKBACK
    result = await session.execute(
        select(Conversation, Contact)
        .join(Contact, Contact.id == Conversation.contact_id)
        .where(
            Conversation.status == ConversationStatus.active,
            Contact.needs_human.is_(False),
            Conversation.id.in_(select(Message.conversation_id).where(Message.created_at >= since)),
        )
        .with_for_update(of=Conversation, skip_locked=True)
    )
    internal = _internal_numbers()
    sent = 0
    for conversation, contact in result.all():
        if contact.wa_id in internal:
            continue
        start = max(since, conversation.handoff_notified_at or since)
        messages = list(
            (
                await session.execute(
                    select(Message)
                    .where(Message.conversation_id == conversation.id, Message.created_at > start)
                    .order_by(Message.created_at)
                )
            ).scalars()
        )
        if not messages or not needs_followup(messages, now, idle):
            continue
        conversation.handoff_notified_at = now
        await conversation_service.notify_staff(
            whatsapp_client,
            contact,
            build_summary(messages, idle),
            student=bool(_tools_used(messages) & _STUDENT_TOOLS),
        )
        logger.info("abandoned_conversation_handed_off", wa_id=contact.wa_id, conversation_id=str(conversation.id))
        sent += 1
    await session.commit()
    return sent


async def run_forever() -> None:
    """Revisa cada `handoff_check_interval_seconds`; un fallo se registra y el ciclo sigue."""
    from app.db.base import async_session_factory

    whatsapp_client = WhatsAppClient()
    while True:
        await asyncio.sleep(settings.handoff_check_interval_seconds)
        try:
            async with async_session_factory() as session:
                await sweep(session, whatsapp_client)
        except Exception:
            logger.exception("abandoned_conversation_sweep_failed")
