import os

os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("WHATSAPP_PHONE_NUMBER_ID", "test")
os.environ.setdefault("WHATSAPP_ACCESS_TOKEN", "test")
os.environ.setdefault("WHATSAPP_APP_SECRET", "test")
os.environ.setdefault("WHATSAPP_VERIFY_TOKEN", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("STRAPI_BASE_URL", "http://localhost:1337")
os.environ.setdefault("ADMIN_API_KEY", "test")

from types import SimpleNamespace

import pytest

from app.services import payment_service
from app.whatsapp.client import WhatsAppClient

PAYMENT = SimpleNamespace(
    reference="PRX-ABC",
    amount_in_cents=15000000,
    contract_number="12345",
    national_id="1020304050",
)
CONTACT = SimpleNamespace(wa_id="573012042870")


class FakeWhatsApp:
    def __init__(self, template_fails: bool = False) -> None:
        self.template_fails = template_fails
        self.images: list[str] = []
        self.templates: list[dict] = []

    async def upload_media(self, data, filename, mime):
        return "media-1"

    async def send_image_by_media_id(self, to, media_id, caption=None):
        self.images.append(to)

    async def send_template(self, **kwargs):
        self.templates.append(kwargs)
        if self.template_fails:
            raise RuntimeError("132001")


@pytest.fixture(autouse=True)
def setup(monkeypatch, tmp_path):
    s = payment_service.settings
    monkeypatch.setattr(s, "document_storage_path", str(tmp_path))
    monkeypatch.setattr(s, "payment_receipt_numbers", "573102394548")
    monkeypatch.setattr(payment_service, "render_payment_receipt", lambda payment, contact: b"png")


@pytest.mark.asyncio
async def test_receipt_goes_to_student_and_service_number_by_template() -> None:
    wa = FakeWhatsApp()
    await payment_service.send_receipt_image(wa, PAYMENT, CONTACT)

    assert wa.images == ["573012042870"]
    assert len(wa.templates) == 1
    sent = wa.templates[0]
    assert sent["to"] == "573102394548"
    assert sent["name"] == "comprobante_pago"
    assert sent["header_image_media_id"] == "media-1"
    assert sent["body_params"] == ["$150.000 COP", "12345", "1020304050", "PRX-ABC"]


@pytest.mark.asyncio
async def test_service_number_falls_back_to_image_when_template_fails() -> None:
    wa = FakeWhatsApp(template_fails=True)
    await payment_service.send_receipt_image(wa, PAYMENT, CONTACT)
    assert wa.images == ["573012042870", "573102394548"]


@pytest.mark.asyncio
async def test_student_still_gets_receipt_when_no_service_number(monkeypatch) -> None:
    monkeypatch.setattr(payment_service.settings, "payment_receipt_numbers", "")
    wa = FakeWhatsApp()
    await payment_service.send_receipt_image(wa, PAYMENT, CONTACT)
    assert wa.images == ["573012042870"]
    assert wa.templates == []


@pytest.mark.asyncio
async def test_template_header_image_payload(monkeypatch) -> None:
    captured = {}

    async def fake_post(self, path, json=None, files=None, data=None):
        captured["json"] = json
        return {}

    monkeypatch.setattr(WhatsAppClient, "_post", fake_post)
    await WhatsAppClient().send_template("573102394548", "comprobante_pago", "es", ["a", "b"], header_image_media_id="m1")
    components = captured["json"]["template"]["components"]
    assert components[0] == {"type": "header", "parameters": [{"type": "image", "image": {"id": "m1"}}]}
    assert components[1]["type"] == "body"
