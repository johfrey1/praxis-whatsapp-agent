import os

os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("WHATSAPP_PHONE_NUMBER_ID", "test")
os.environ.setdefault("WHATSAPP_ACCESS_TOKEN", "test")
os.environ.setdefault("WHATSAPP_APP_SECRET", "test")
os.environ.setdefault("WHATSAPP_VERIFY_TOKEN", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("STRAPI_BASE_URL", "http://localhost:1337")
os.environ.setdefault("ADMIN_API_KEY", "test")

import pytest
from fastapi.testclient import TestClient

from app.db.base import get_session
from app.main import app
from app.routers import admin_payments
from app.whatsapp.client import WhatsAppClient

HEADERS = {"X-API-Key": "test"}
BODY = {
    "wa_id": "573001112233",
    "student_name": "Ana Pérez",
    "national_id": "1020304050",
    "contract_number": "12345",
    "account_number": "001",
}


@pytest.mark.asyncio
async def test_send_template_adds_url_button_parameter(monkeypatch) -> None:
    captured = {}

    async def fake_post(self, path, json=None, files=None, data=None):
        captured["json"] = json
        return {}

    monkeypatch.setattr(WhatsAppClient, "_post", fake_post)
    await WhatsAppClient().send_template(
        "573001112233", "pago_cuota", "es", ["Ana", "12345", "001"], url_button_suffix="test_AbC123"
    )
    components = captured["json"]["template"]["components"]
    assert components[0]["parameters"] == [
        {"type": "text", "text": "Ana"},
        {"type": "text", "text": "12345"},
        {"type": "text", "text": "001"},
    ]
    assert components[1] == {
        "type": "button",
        "sub_type": "url",
        "index": "0",
        "parameters": [{"type": "text", "text": "test_AbC123"}],
    }


@pytest.mark.asyncio
async def test_send_template_without_button_keeps_only_body(monkeypatch) -> None:
    captured = {}

    async def fake_post(self, path, json=None, files=None, data=None):
        captured["json"] = json
        return {}

    monkeypatch.setattr(WhatsAppClient, "_post", fake_post)
    await WhatsAppClient().send_template("573001112233", "aviso_equipo", "es", ["hola"])
    assert [c["type"] for c in captured["json"]["template"]["components"]] == ["body"]


class _FakeSession:
    async def rollback(self) -> None:
        pass


def _client(monkeypatch, payments_enabled: bool = True) -> TestClient:
    async def fake_session():
        yield _FakeSession()

    app.dependency_overrides[get_session] = fake_session
    monkeypatch.setattr(type(admin_payments.settings), "payments_enabled", property(lambda self: payments_enabled))
    return TestClient(app)


def test_reminder_requires_api_key(monkeypatch) -> None:
    client = _client(monkeypatch)
    try:
        assert client.post("/admin/payments/reminders", json=BODY).status_code in (401, 422)
    finally:
        app.dependency_overrides.clear()


def test_reminder_rejects_non_colombian_number(monkeypatch) -> None:
    client = _client(monkeypatch)
    try:
        response = client.post("/admin/payments/reminders", json={**BODY, "wa_id": "12025550123"}, headers=HEADERS)
        assert response.status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_reminder_unavailable_when_payments_disabled(monkeypatch) -> None:
    client = _client(monkeypatch, payments_enabled=False)
    try:
        assert client.post("/admin/payments/reminders", json=BODY, headers=HEADERS).status_code == 503
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_send_installment_reminder_uses_template_with_link_id(monkeypatch) -> None:
    from types import SimpleNamespace

    from app.services import conversation_service, payment_service

    payment = SimpleNamespace(
        contract_number="12345",
        account_number="001",
        wompi_payment_link_id="test_AbC123",
        payment_url="https://checkout.wompi.co/l/test_AbC123",
        reference="PRX-ABC",
    )
    sent, recorded = {}, []

    async def fake_conversation(session, contact):
        return SimpleNamespace(id="conv")

    async def fake_create(session, wompi, contact, conversation, national_id, contract, account):
        return payment

    async def fake_record(session, conversation, **kwargs):
        recorded.append(kwargs)

    class FakeWhatsApp:
        async def send_template(self, **kwargs):
            sent.update(kwargs)

    monkeypatch.setattr(conversation_service, "get_or_create_active_conversation", fake_conversation)
    monkeypatch.setattr(conversation_service, "record_message", fake_record)
    monkeypatch.setattr(payment_service, "create_installment_payment", fake_create)

    result = await payment_service.send_installment_reminder(
        None, FakeWhatsApp(), None, SimpleNamespace(wa_id="573001112233"), "Ana Pérez", "1020304050", "12345", "001"
    )

    assert result is payment
    assert sent["name"] == "pago_cuota"
    assert sent["body_params"] == ["Ana", "12345", "001"]
    assert sent["url_button_suffix"] == "test_AbC123"
    assert recorded and "Recordatorio de cuota" in recorded[0]["content"]
