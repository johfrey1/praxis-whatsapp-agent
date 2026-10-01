import pytest

from app.whatsapp import client as wa_client
from app.whatsapp.client import WhatsAppClient


class RecordingClient(WhatsAppClient):
    def __init__(self) -> None:
        super().__init__()
        self.payloads: list[dict] = []

    async def _post(self, path, json=None, files=None, data=None):
        self.payloads.append(json)
        return {}


@pytest.mark.asyncio
async def test_staff_message_uses_free_text_without_template(monkeypatch) -> None:
    monkeypatch.setattr(wa_client.settings, "staff_template_name", "")
    c = RecordingClient()
    await c.send_staff_message("573001112233", "hola")
    assert c.payloads[0]["type"] == "text"


@pytest.mark.asyncio
async def test_staff_message_uses_template_with_flattened_param(monkeypatch) -> None:
    monkeypatch.setattr(wa_client.settings, "staff_template_name", "aviso_equipo")
    monkeypatch.setattr(wa_client.settings, "staff_template_language", "es")
    c = RecordingClient()
    await c.send_staff_message("573001112233", "🔔 Nuevo lead\nContacto:    Ana\n\n(573001112233)")
    payload = c.payloads[0]
    assert payload["type"] == "template"
    assert payload["template"]["name"] == "aviso_equipo"
    assert payload["template"]["language"] == {"code": "es"}
    text = payload["template"]["components"][0]["parameters"][0]["text"]
    assert "\n" not in text and "  " not in text
    assert text == "🔔 Nuevo lead | Contacto: Ana | (573001112233)"


class FailingTemplateClient(RecordingClient):
    async def _post(self, path, json=None, files=None, data=None):
        self.payloads.append(json)
        if json["type"] == "template":
            raise RuntimeError("132001 template does not exist")
        return {}


@pytest.mark.asyncio
async def test_staff_message_falls_back_to_text_when_template_fails(monkeypatch) -> None:
    monkeypatch.setattr(wa_client.settings, "staff_template_name", "aviso_equipo")
    c = FailingTemplateClient()
    await c.send_staff_message("573001112233", "🔔 Nuevo lead\nContacto: Ana")
    assert [p["type"] for p in c.payloads] == ["template", "text"]
    assert c.payloads[1]["text"]["body"] == "🔔 Nuevo lead\nContacto: Ana"
