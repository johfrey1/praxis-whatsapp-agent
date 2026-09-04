from dataclasses import dataclass

from app.schemas.webhook import WebhookEnvelope


@dataclass
class IncomingMessage:
    wa_message_id: str
    from_wa_id: str
    profile_name: str | None
    message_type: str
    text: str | None = None
    interactive_reply_id: str | None = None
    media_id: str | None = None
    media_mime_type: str | None = None


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

            contacts_by_wa_id = {c.get("wa_id"): c for c in value.contacts}

            for msg in value.messages:
                wa_id = msg.get("from", "")
                profile_name = (contacts_by_wa_id.get(wa_id) or {}).get("profile", {}).get("name")
                msg_type = msg.get("type", "text")

                incoming = IncomingMessage(
                    wa_message_id=msg.get("id", ""),
                    from_wa_id=wa_id,
                    profile_name=profile_name,
                    message_type=msg_type,
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
                elif msg_type in ("document", "image", "audio"):
                    media = msg.get(msg_type, {})
                    incoming.media_id = media.get("id")
                    incoming.media_mime_type = media.get("mime_type")
                    incoming.text = media.get("caption")

                results.append(incoming)

    return results
