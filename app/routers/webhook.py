import json
from datetime import datetime, timezone

from fastapi import APIRouter, Header, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends

from app.agent.claude_agent import run_agent_turn
from app.agent.menu import MEDIA_FALLBACK_TEXT, MENU_TRIGGER_WORDS, send_main_menu
from app.agent.tools import ToolContext
from app.agent.welcome import PROSPECT_MENU_IDS, build_welcome, is_plain_greeting
from app.config import get_settings
from app.db.base import get_session
from app.db.models import MessageDirection, MessageType
from app.logging_config import get_logger
from app.security import verify_webhook_signature
from app.services import manual_payment_service, conversation_service
from app.strapi.client import StrapiClient
from app.whatsapp.client import WhatsAppClient
from app.whatsapp.parser import parse_failed_statuses, parse_incoming_messages
from app.wompi.client import WompiClient

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

    for failed in parse_failed_statuses(payload):
        logger.warning(
            "whatsapp_delivery_failed",
            wa_message_id=failed.wa_message_id,
            recipient_id=failed.recipient_id,
            error_code=failed.error_code,
            error_title=failed.error_title,
            error_details=failed.error_details,
        )

    incoming_messages = parse_incoming_messages(payload)
    if not incoming_messages:
        return {"status": "ignored"}

    whatsapp_client = WhatsAppClient()
    strapi_client = StrapiClient()
    wompi_client = WompiClient()

    for incoming in incoming_messages:
        if await conversation_service.message_already_processed(session, incoming.wa_message_id):
            logger.info("duplicate_message_ignored", wa_message_id=incoming.wa_message_id)
            continue
        try:
            await _handle_incoming_message(session, whatsapp_client, strapi_client, wompi_client, incoming)
            await session.commit()
        except Exception:
            await session.rollback()
            logger.exception("failed_to_process_message", wa_message_id=incoming.wa_message_id)
            await _send_failure_notice(whatsapp_client, incoming.from_wa_id)

    return {"status": "ok"}


async def _handle_payment_proof(session, whatsapp_client, payment, contact, media_id: str) -> str:
    """La captura del pago (Bre-B, Nequi o Daviplata) queda como evidencia, en espera de aprobación, y va al gestor."""
    try:
        await manual_payment_service.receive_proof(session, whatsapp_client, payment, contact, media_id)
    except manual_payment_service.ManualPaymentError:
        return "Ese archivo no lo puedo recibir. Envíame la captura del pago como foto o como PDF, por favor."
    except Exception:
        logger.exception("manual_payment_proof_failed", reference=payment.reference)
        return "No pude recibir tu captura. ¿Me la envías de nuevo en un momento?"
    return (
        f"Recibí tu comprobante. Tu pago {payment.reference} quedó en espera de aprobación; "
        "ya se lo pasé al gestor de estudiantes y apenas lo confirme te aviso por aquí."
    )


async def _send_failure_notice(whatsapp_client: WhatsAppClient, wa_id: str) -> None:
    """Si algo falla al procesar, el usuario no debe quedarse sin respuesta."""
    try:
        await whatsapp_client.send_text(
            to=wa_id,
            body="Tuve un inconveniente para responderte. ¿Me escribes de nuevo en un momento? "
            "Si es urgente, escribe *asesor* y te comunico con una persona.",
        )
    except Exception:
        logger.warning("failure_notice_not_sent", wa_id=wa_id)


async def _handle_incoming_message(session, whatsapp_client, strapi_client, wompi_client, incoming) -> None:
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

    # El gestor aprueba o rechaza pagos por transferencia respondiendo por WhatsApp.
    manager_reply = await manual_payment_service.handle_manager_command(
        session, whatsapp_client, contact.wa_id, incoming.text
    )
    if manager_reply is not None:
        await whatsapp_client.send_text(to=contact.wa_id, body=manager_reply)
        await conversation_service.record_message(
            session,
            conversation,
            direction=MessageDirection.outbound,
            message_type=MessageType.text,
            content=manager_reply,
        )
        return

    if was_new:
        contact.human_notified_at = datetime.now(timezone.utc)
        await conversation_service.notify_staff(whatsapp_client, contact, "Nuevo contacto en WhatsApp")

    user_text = incoming.text or ""
    should_send_menu = not was_new and user_text.strip().lower() in MENU_TRIGGER_WORDS
    if incoming.interactive_reply_id in PROSPECT_MENU_IDS:
        # Eligió Cursos/Horarios/Precios: es un prospecto, que el agente no vuelva a preguntarlo.
        user_text = f"{user_text} (eligió esta opción del menú: quiere aprender inglés en Praxis)"

    proof_payment = None
    if incoming.message_type in ("image", "document") and incoming.media_id and not was_new:
        proof_payment = await manual_payment_service.find_awaiting_proof(session, contact)

    if proof_payment is not None:
        reply = await _handle_payment_proof(session, whatsapp_client, proof_payment, contact, incoming.media_id)
        await whatsapp_client.send_text(to=contact.wa_id, body=reply)
        await conversation_service.record_message(
            session,
            conversation,
            direction=MessageDirection.outbound,
            message_type=MessageType.text,
            content=reply,
        )
    elif was_new and is_plain_greeting(user_text):
        # Bienvenida fija y personalizada: instantánea y sin menú duplicado.
        welcome_text = build_welcome(contact.profile_name)
        await whatsapp_client.send_text(to=contact.wa_id, body=welcome_text)
        await conversation_service.record_message(
            session,
            conversation,
            direction=MessageDirection.outbound,
            message_type=MessageType.text,
            content=welcome_text,
        )
    elif not user_text and incoming.message_type in MEDIA_FALLBACK_TEXT:
        # Audio/imagen/documento SIN texto/caption: antes se ignoraba en silencio.
        # Respondemos con un mensaje claro en vez de dejar al usuario sin respuesta.
        fallback_text = MEDIA_FALLBACK_TEXT[incoming.message_type]
        await whatsapp_client.send_text(to=contact.wa_id, body=fallback_text)
        await conversation_service.record_message(
            session,
            conversation,
            direction=MessageDirection.outbound,
            message_type=MessageType.text,
            content=fallback_text,
        )
    elif user_text:
        history = await conversation_service.get_recent_messages(session, conversation, limit=20)
        tool_ctx = ToolContext(
            session=session,
            contact=contact,
            conversation=conversation,
            whatsapp_client=whatsapp_client,
            strapi_client=strapi_client,
            wompi_client=wompi_client,
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
