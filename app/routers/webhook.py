import base64
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.claude_agent import run_agent_turn
from app.agent.menu import MEDIA_FALLBACK_TEXT, MENU_TRIGGER_WORDS, send_main_menu
from app.agent.tools import ToolContext
from app.agent.welcome import PROSPECT_MENU_IDS, build_welcome, is_plain_greeting
from app.config import get_settings
from app.db.base import get_session
from app.db.models import Message, MessageDirection, MessageType, WebhookEvent
from app.logging_config import get_logger
from app.security import verify_webhook_signature
from app.services import manual_payment_service, conversation_service
from app.strapi.client import StrapiClient
from app.whatsapp.client import WhatsAppClient
from app.whatsapp.parser import parse_incoming_messages, parse_statuses
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
    "video": MessageType.video,
    "sticker": MessageType.sticker,
    "location": MessageType.location,
    "contacts": MessageType.contacts,
    "reaction": MessageType.reaction,
    "button": MessageType.interactive,
}

FAILURE_NOTICE = (
    "Tuve un inconveniente para responderte. ¿Me escribes de nuevo en un momento? "
    "Si es urgente, escribe *asesor* y te comunico con una persona."
)
_CLAUDE_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
# Claude acepta imágenes de hasta 5 MB codificadas en base64 (~3,75 MB del archivo original).
_MAX_MEDIA_BYTES = 3_750_000


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

    statuses = parse_statuses(payload)
    incoming_messages = parse_incoming_messages(payload)

    # Bitácora: el payload queda guardado antes de procesar nada, aunque luego algo falle.
    event = WebhookEvent(payload=payload, messages_count=len(incoming_messages), statuses_count=len(statuses))
    session.add(event)

    for failed in (st for st in statuses if st.status == "failed"):
        logger.warning(
            "whatsapp_delivery_failed",
            wa_message_id=failed.wa_message_id,
            recipient_id=failed.recipient_id,
            error_code=failed.error_code,
            error_title=failed.error_title,
            error_details=failed.error_details,
        )
    if statuses:
        await conversation_service.update_delivery_statuses(session, statuses)

    if not incoming_messages:
        event.outcome = "processed" if statuses else "ignored"
        await session.commit()
        return {"status": "ignored"}

    await session.flush()
    event_id = event.id
    await session.commit()

    whatsapp_client = WhatsAppClient()
    strapi_client = StrapiClient()
    wompi_client = WompiClient()

    errors: list[str] = []
    for incoming in incoming_messages:
        error = await _process_incoming(session, whatsapp_client, strapi_client, wompi_client, incoming)
        if error:
            errors.append(f"{incoming.wa_message_id}: {error}")

    if not errors:
        outcome = "processed"
    elif len(errors) < len(incoming_messages):
        outcome = "partial"
    else:
        outcome = "failed"
    await session.execute(
        update(WebhookEvent)
        .where(WebhookEvent.id == event_id)
        .values(outcome=outcome, error="\n".join(errors)[:4000] or None)
    )
    await session.commit()
    return {"status": "ok"}


async def _process_incoming(session, whatsapp_client, strapi_client, wompi_client, incoming) -> str | None:
    """Guarda el mensaje (y al cliente) y luego responde. Devuelve el error para la bitácora, o None."""
    if not incoming.from_wa_id:
        logger.error("incoming_message_without_sender", wa_message_id=incoming.wa_message_id)
        return "sin remitente (ni celular ni BSUID)"
    if await conversation_service.message_already_processed(session, incoming.wa_message_id):
        logger.info("duplicate_message_ignored", wa_message_id=incoming.wa_message_id)
        return None

    # 1) Guardar primero: el cliente y su mensaje quedan registrados aunque la respuesta falle, y un
    #    reintento de Meta mientras Claude piensa se detecta como duplicado en vez de responderse dos veces.
    try:
        contact, was_new, conversation = await _record_incoming(session, incoming)
        await session.commit()
    except IntegrityError:
        await session.rollback()
        logger.info("duplicate_message_ignored", wa_message_id=incoming.wa_message_id)
        return None
    except Exception as exc:
        await session.rollback()
        logger.exception("failed_to_record_message", wa_message_id=incoming.wa_message_id)
        await _send_failure_notice(whatsapp_client, incoming.from_wa_id)
        return f"no se pudo guardar: {exc!r}"[:500]

    conversation_id = conversation.id
    # 2) Responder.
    try:
        await _handle_incoming_message(
            session, whatsapp_client, strapi_client, wompi_client, incoming, contact, was_new, conversation
        )
        await session.commit()
    except Exception as exc:
        await session.rollback()
        logger.exception("failed_to_process_message", wa_message_id=incoming.wa_message_id)
        notice_id = await _send_failure_notice(whatsapp_client, incoming.from_wa_id)
        if notice_id:
            session.add(
                Message(
                    conversation_id=conversation_id,
                    wa_message_id=notice_id,
                    direction=MessageDirection.outbound,
                    message_type=MessageType.system,
                    content=FAILURE_NOTICE,
                    delivery_status="sent",
                )
            )
            await session.commit()
        return f"{exc!r}"[:500]
    return None


async def _record_incoming(session, incoming):
    contact, was_new = await conversation_service.get_or_create_contact(
        session, incoming.from_wa_id, incoming.profile_name, user_id=incoming.user_id, username=incoming.username
    )
    conversation = await conversation_service.get_or_create_active_conversation(session, contact)
    await conversation_service.record_message(
        session,
        conversation,
        direction=MessageDirection.inbound,
        message_type=_MESSAGE_TYPE_MAP.get(incoming.message_type, MessageType.other),
        content=incoming.text,
        wa_message_id=incoming.wa_message_id,
        media_id=incoming.media_id,
    )
    return contact, was_new, conversation


async def _reply(session, whatsapp_client, conversation, contact, text: str, tool_calls: dict | None = None) -> None:
    """Envía texto y lo guarda con el id de Meta, para luego saber si llegó y si lo leyó."""
    response = await whatsapp_client.send_text(to=contact.wa_id, body=text)
    wamid = ((response or {}).get("messages") or [{}])[0].get("id") if isinstance(response, dict) else None
    message = await conversation_service.record_message(
        session,
        conversation,
        direction=MessageDirection.outbound,
        message_type=MessageType.text,
        content=text,
        wa_message_id=wamid,
        tool_calls=tool_calls,
    )
    if wamid:
        message.delivery_status = "sent"


async def _media_for_claude(whatsapp_client, incoming) -> dict | None:
    """Descarga la imagen o PDF que envió el usuario para que Claude la vea."""
    try:
        data, mime_type = await whatsapp_client.download_media(incoming.media_id)
    except Exception:
        logger.warning("media_download_failed", wa_message_id=incoming.wa_message_id)
        return None
    mime_type = mime_type.split(";")[0].strip()
    if len(data) > _MAX_MEDIA_BYTES:
        logger.info("media_too_large_for_claude", size=len(data), mime_type=mime_type)
        return None
    encoded = base64.standard_b64encode(data).decode()
    if mime_type in _CLAUDE_IMAGE_TYPES:
        return {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": encoded}}
    if mime_type == "application/pdf":
        return {"type": "document", "source": {"type": "base64", "media_type": mime_type, "data": encoded}}
    return None


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


async def _send_failure_notice(whatsapp_client: WhatsAppClient, wa_id: str) -> str | None:
    """Si algo falla al procesar, el usuario no debe quedarse sin respuesta. Devuelve el id de Meta."""
    try:
        response = await whatsapp_client.send_text(to=wa_id, body=FAILURE_NOTICE)
        return ((response or {}).get("messages") or [{}])[0].get("id") if isinstance(response, dict) else None
    except Exception:
        logger.warning("failure_notice_not_sent", wa_id=wa_id)
        return None


async def _handle_incoming_message(
    session, whatsapp_client, strapi_client, wompi_client, incoming, contact, was_new, conversation
) -> None:
    if incoming.message_type == "reaction":
        return  # Un 👍 a un mensaje queda en la bitácora; no amerita respuesta.

    try:
        # "Escribiendo..." mientras se arma la respuesta.
        await whatsapp_client.mark_as_read(incoming.wa_message_id, typing=True)
    except Exception:
        logger.warning("mark_as_read_failed", wa_message_id=incoming.wa_message_id)

    # El gestor aprueba o rechaza pagos por transferencia respondiendo por WhatsApp.
    manager_reply = await manual_payment_service.handle_manager_command(
        session, whatsapp_client, contact.wa_id, incoming.text
    )
    if manager_reply is not None:
        await _reply(session, whatsapp_client, conversation, contact, manager_reply)
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

    # Fotos, capturas y PDF que no son el comprobante de un pago en curso: Claude los ve.
    attachments: list[dict] = []
    if proof_payment is None and incoming.message_type in ("image", "document") and incoming.media_id:
        attachment = await _media_for_claude(whatsapp_client, incoming)
        if attachment is not None:
            attachments.append(attachment)
            if not user_text:
                kind = "una imagen" if attachment["type"] == "image" else "un PDF"
                user_text = f"(te envió {kind} sin texto)"

    if proof_payment is not None:
        reply = await _handle_payment_proof(session, whatsapp_client, proof_payment, contact, incoming.media_id)
        await _reply(session, whatsapp_client, conversation, contact, reply)
    elif was_new and not attachments and is_plain_greeting(user_text):
        # Bienvenida fija y personalizada: instantánea y sin menú duplicado.
        await _reply(session, whatsapp_client, conversation, contact, build_welcome(contact.profile_name))
    elif not user_text and incoming.message_type in MEDIA_FALLBACK_TEXT:
        # Audio/video, o imagen/documento que no se pudo leer, sin texto: respuesta clara en vez de silencio.
        await _reply(session, whatsapp_client, conversation, contact, MEDIA_FALLBACK_TEXT[incoming.message_type])
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
        reply_text, tool_calls = await run_agent_turn(user_text, history[:-1], tool_ctx, attachments=attachments)
        await _reply(
            session,
            whatsapp_client,
            conversation,
            contact,
            reply_text,
            tool_calls={"calls": tool_calls} if tool_calls else None,
        )

    if should_send_menu:
        await send_main_menu(whatsapp_client, contact.wa_id)
