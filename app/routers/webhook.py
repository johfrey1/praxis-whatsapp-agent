import json
from datetime import datetime, timezone

from fastapi import APIRouter, Header, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends

from app.agent.claude_agent import run_agent_turn
from app.agent.menu import MENU_TRIGGER_WORDS, send_main_menu
from app.agent.tools import ToolContext
from app.config import get_settings
from app.db.base import get_session
from app.db.models import MessageDirection, MessageType
from app.logging_config import get_logger
from app.security import verify_webhook_signature
from app.services import conversation_service
from app.strapi.client import StrapiClient
from app.whatsapp.client import WhatsAppClient
from app.whatsapp.parser import parse_incoming_messages

router = APIRouter()
settings = get_settings()
logger = get_logger(__name__)

_MESSAGE_TYPE_MAP = {
    "text": MessageType.text,
    "interactive": MessageType.interactive,
    "document": MessageType.document,
    "image": MessageType.image,
    "audio": MessageType.audio,
}


@router.get("/webhook")
async def verify_webhook(request: Request) -> Response:
    params = request.query_params
    if params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == settings.whatsapp_verify_token:
        return Response(content=params.get("hub.challenge", ""), media_type="text/plain")
    raise HTTPException(status.HTTP_403_FORBIDDEN, "Verification failed")


@router.post("/webhook")
async def receive_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> dict:
    raw_body = await request.body()
    verify_webhook_signature(raw_body, x_hub_signature_256)
    payload = json.loads(raw_body)

    incoming_messages = parse_incoming_messages(payload)
    if not incoming_messages:
        return {"status": "ignored"}

    whatsapp_client = WhatsAppClient()
    strapi_client = StrapiClient()

    for incoming in incoming_messages:
        try:
            await _handle_incoming_message(session, whatsapp_client, strapi_client, incoming)
            await session.commit()
        except Exception:
            await session.rollback()
            logger.exception("failed_to_process_message", wa_message_id=incoming.wa_message_id)

    return {"status": "ok"}


async def _handle_incoming_message(session, whatsapp_client, strapi_client, incoming) -> None:
    contact, was_new = await conversation_service.get_or_create_contact(session, incoming.from_wa_id, incoming.profile_name)
    conversation = await conversation_service.get_or_create_active_conversation(session, contact)

    await conversation_service.record_message(
        session,
        conversation,
        direction=MessageDirection.inbound,
        message_type=_MESSAGE_TYPE_MAP.get(incoming.message_type, MessageType.text),
        content=incoming.text,
        wa_message_id=incoming.wa_message_id,
        media_id=incoming.media_id,
    )

    try:
        await whatsapp_client.mark_as_read(incoming.wa_message_id)
    except Exception:
        logger.warning("mark_as_read_failed", wa_message_id=incoming.wa_message_id)

    if was_new:
        contact.human_notified_at = datetime.now(timezone.utc)
        await conversation_service.notify_staff(whatsapp_client, contact, "Nuevo contacto en WhatsApp")

    user_text = incoming.text or ""
    should_send_menu = was_new or user_text.strip().lower() in MENU_TRIGGER_WORDS

    if user_text:
        history = await conversation_service.get_recent_messages(session, conversation, limit=20)
        tool_ctx = ToolContext(
            session=session,
            contact=contact,
            conversation=conversation,
            whatsapp_client=whatsapp_client,
            strapi_client=strapi_client,
        )
        reply_text, tool_calls = await run_agent_turn(user_text, history[:-1], tool_ctx)

        await whatsapp_client.send_text(to=contact.wa_id, body=reply_text)
        await conversation_service.record_message(
            session,
            conversation,
            direction=MessageDirection.outbound,
            message_type=MessageType.text,
            content=reply_text,
            tool_calls={"calls": tool_calls} if tool_calls else None,
        )

    if should_send_menu:
        await send_main_menu(whatsapp_client, contact.wa_id)
