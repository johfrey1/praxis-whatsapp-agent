"""Cliente para la WhatsApp Cloud API (Meta Graph API)."""

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import get_settings
from app.logging_config import get_logger

settings = get_settings()
logger = get_logger(__name__)

_retry = retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8))


class WhatsAppClient:
    def __init__(self) -> None:
        self._base_url = f"{settings.graph_api_base_url}/{settings.whatsapp_phone_number_id}"
        self._headers = {"Authorization": f"Bearer {settings.whatsapp_access_token}"}

    @_retry
    async def _post(self, path: str, json: dict | None = None, files: dict | None = None, data: dict | None = None) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(f"{self._base_url}/{path}", headers=self._headers, json=json, files=files, data=data)
        if response.is_error:
            logger.error("whatsapp_api_error", path=path, status=response.status_code, body=response.text)
        response.raise_for_status()
        return response.json()

    async def mark_as_read(self, wa_message_id: str) -> None:
        await self._post(
            "messages",
            json={"messaging_product": "whatsapp", "status": "read", "message_id": wa_message_id},
        )

    async def send_text(self, to: str, body: str, preview_url: bool = False) -> dict:
        return await self._post(
            "messages",
            json={
                "messaging_product": "whatsapp",
                "to": to,
                "type": "text",
                "text": {"body": body, "preview_url": preview_url},
            },
        )

    async def send_template(self, to: str, name: str, language: str, body_params: list[str]) -> dict:
        components = [{"type": "body", "parameters": [{"type": "text", "text": p} for p in body_params]}]
        return await self._post(
            "messages",
            json={
                "messaging_product": "whatsapp",
                "to": to,
                "type": "template",
                "template": {"name": name, "language": {"code": language}, "components": components},
            },
        )

    async def send_staff_message(self, to: str, text: str) -> dict:
        """Aviso interno al personal. Con `staff_template_name` configurada usa la plantilla (llega
        fuera de la ventana de 24 h) y, si esta falla, cae a texto libre; sin ella, texto libre."""
        if not settings.staff_template_name:
            return await self.send_text(to=to, body=text)
        # Meta rechaza variables con saltos de línea, tabulaciones o 4+ espacios seguidos.
        flat = " | ".join(line.strip() for line in text.splitlines() if line.strip())
        flat = " ".join(flat.split())[:1000]
        try:
            return await self.send_template(
                to=to, name=settings.staff_template_name, language=settings.staff_template_language, body_params=[flat]
            )
        except Exception:
            # Plantilla no aprobada/inexistente (132001) u otro fallo: mejor texto libre (llega si el
            # destinatario escribió en las últimas 24 h) que perder el aviso.
            logger.warning("staff_template_failed_falling_back_to_text", template=settings.staff_template_name, to=to)
            return await self.send_text(to=to, body=text)

    async def send_interactive_list(self, to: str, header: str, body: str, button_text: str, sections: list[dict]) -> dict:
        """sections: [{"title": str, "rows": [{"id": str, "title": str, "description": str}]}]"""
        return await self._post(
            "messages",
            json={
                "messaging_product": "whatsapp",
                "to": to,
                "type": "interactive",
                "interactive": {
                    "type": "list",
                    "header": {"type": "text", "text": header},
                    "body": {"text": body},
                    "action": {"button": button_text, "sections": sections},
                },
            },
        )

    async def send_cta_url(
        self, to: str, header: str, body: str, button_text: str, url: str, footer: str | None = None
    ) -> dict:
        """Mensaje con botón que abre `url` en el navegador interno de WhatsApp.
        Límites de Meta: header 60, body 1024, footer 60 y button_text 20 caracteres."""
        interactive: dict = {
            "type": "cta_url",
            "header": {"type": "text", "text": header},
            "body": {"text": body},
            "action": {"name": "cta_url", "parameters": {"display_text": button_text, "url": url}},
        }
        if footer:
            interactive["footer"] = {"text": footer}
        return await self._post(
            "messages",
            json={"messaging_product": "whatsapp", "to": to, "type": "interactive", "interactive": interactive},
        )

    async def send_document_by_media_id(self, to: str, media_id: str, filename: str, caption: str | None = None) -> dict:
        return await self._post(
            "messages",
            json={
                "messaging_product": "whatsapp",
                "to": to,
                "type": "document",
                "document": {"id": media_id, "filename": filename, "caption": caption},
            },
        )

    async def send_image_by_media_id(self, to: str, media_id: str, caption: str | None = None) -> dict:
        image: dict = {"id": media_id}
        if caption:
            image["caption"] = caption
        return await self._post(
            "messages",
            json={"messaging_product": "whatsapp", "to": to, "type": "image", "image": image},
        )

    async def upload_media(self, file_bytes: bytes, filename: str, mime_type: str) -> str:
        """Sube un archivo y devuelve el media_id (válido ~30 días) para reusar en envíos."""
        files = {"file": (filename, file_bytes, mime_type)}
        data = {"messaging_product": "whatsapp", "type": mime_type}
        result = await self._post("media", files=files, data=data)
        return result["id"]

    async def download_media(self, media_id: str) -> tuple[bytes, str]:
        async with httpx.AsyncClient(timeout=30) as client:
            meta_resp = await client.get(f"{settings.graph_api_base_url}/{media_id}", headers=self._headers)
        meta_resp.raise_for_status()
        meta = meta_resp.json()
        async with httpx.AsyncClient(timeout=60) as client:
            file_resp = await client.get(meta["url"], headers=self._headers)
        file_resp.raise_for_status()
        return file_resp.content, meta.get("mime_type", "application/octet-stream")
