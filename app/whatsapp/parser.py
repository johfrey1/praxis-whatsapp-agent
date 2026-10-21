from dataclasses import dataclass

from app.schemas.webhook import WebhookEnvelope

MEDIA_TYPES = ("document", "image", "audio", "video", "sticker")


@dataclass
class IncomingMessage:
    wa_message_id: str
    # Dirección para responder: el celular o, si Meta no lo comparte, el BSUID del usuario.
    from_wa_id: str
    profile_name: str | None
    message_type: str
    text: str | None = None
    interactive_reply_id: str | None = None
    media_id: str | None = None
    media_mime_type: str | None = None
    phone: str | None = None
    user_id: str | None = None
    username: str | None = None


def _sender_contact(contacts: list[dict], phone: str | None, user_id: str | None) -> dict:
    for contact in contacts:
        if (phone and contact.get("wa_id") == phone) or (user_id and contact.get("user_id") == user_id):
            return contact
    # Un webhook trae un solo remitente: si no hubo coincidencia exacta, es ese.
    return contacts[0] if len(contacts) == 1 else {}


def parse_incoming_messages(payload: dict) -> list[IncomingMessage]:
    """Extrae mensajes normalizados de un payload de webhook. Ignora eventos
    de tipo 'status' (delivered/read) que no traen mensajes de usuario."""
    envelope = WebhookEnvelope.model_validate(payload)
    results: list[IncomingMessage] = []

    for entry in envelope.entry:
        for change in entry.changes:
            value = change.value
            if not value or not value.messages:
                continue

            for msg in value.messages:
                # Con nombres de usuario de WhatsApp, Meta omite `from` (el celular) si no ha habido
                # conversación con ese número en 30 días; `from_user_id` (BSUID) llega siempre.
                phone = msg.get("from") or None
                contact = _sender_contact(value.contacts, phone, msg.get("from_user_id"))
                phone = phone or contact.get("wa_id") or None
                user_id = msg.get("from_user_id") or contact.get("user_id") or None
                profile = contact.get("profile") or {}
                msg_type = msg.get("type", "text")

                incoming = IncomingMessage(
                    wa_message_id=msg.get("id", ""),
                    from_wa_id=phone or user_id or "",
                    profile_name=profile.get("name"),
                    message_type=msg_type,
                    phone=phone,
                    user_id=user_id,
                    username=profile.get("username") or contact.get("username"),
                )

                if msg_type == "text":
                    incoming.text = msg.get("text", {}).get("body")
                elif msg_type == "interactive":
                    interactive = msg.get("interactive", {})
                    if interactive.get("type") == "list_reply":
                        incoming.interactive_reply_id = interactive["list_reply"]["id"]
                        incoming.text = interactive["list_reply"].get("title")
                    elif interactive.get("type") == "button_reply":
                        incoming.interactive_reply_id = interactive["button_reply"]["id"]
                        incoming.text = interactive["button_reply"].get("title")
                elif msg_type == "button":
                    # Botón de respuesta rápida de una plantilla (recordatorios, avisos).
                    button = msg.get("button", {})
                    incoming.interactive_reply_id = button.get("payload")
                    incoming.text = button.get("text")
                elif msg_type in MEDIA_TYPES:
                    media = msg.get(msg_type, {})
                    incoming.media_id = media.get("id")
                    incoming.media_mime_type = media.get("mime_type")
                    incoming.text = media.get("caption")
                elif msg_type == "location":
                    loc = msg.get("location", {})
                    place = ", ".join(p for p in (loc.get("name"), loc.get("address")) if p)
                    incoming.text = f"(compartió su ubicación{': ' + place if place else ''})"
                elif msg_type == "contacts":
                    shared = msg.get("contacts") or []
                    names = ", ".join((c.get("name") or {}).get("formatted_name", "") for c in shared).strip(", ")
                    phones = ", ".join(p.get("phone", "") for c in shared for p in (c.get("phones") or []))
                    incoming.text = f"(compartió un contacto: {names} {phones})".replace("  ", " ")

                results.append(incoming)

    return results


@dataclass
class DeliveryStatus:
    wa_message_id: str
    status: str
    recipient_id: str
    error_code: int | None = None
    error_title: str | None = None
    error_details: str | None = None


def parse_statuses(payload: dict) -> list[DeliveryStatus]:
    """Estados de los mensajes que enviamos: sent, delivered, read o failed."""
    envelope = WebhookEnvelope.model_validate(payload)
    results: list[DeliveryStatus] = []
    for entry in envelope.entry:
        for change in entry.changes:
            if not change.value:
                continue
            for st in change.value.statuses:
                error = (st.get("errors") or [{}])[0]
                results.append(
                    DeliveryStatus(
                        wa_message_id=st.get("id", ""),
                        status=st.get("status", ""),
                        recipient_id=st.get("recipient_id") or st.get("recipient_user_id") or "",
                        error_code=error.get("code"),
                        error_title=error.get("title"),
                        error_details=(error.get("error_data") or {}).get("details"),
                    )
                )
    return results


def parse_failed_statuses(payload: dict) -> list[DeliveryStatus]:
    """Envíos que Meta aceptó (HTTP 200) pero no pudo entregar, p. ej. 131047: el destinatario
    no ha escrito en las últimas 24 h y el mensaje no es una plantilla."""
    return [st for st in parse_statuses(payload) if st.status == "failed"]
