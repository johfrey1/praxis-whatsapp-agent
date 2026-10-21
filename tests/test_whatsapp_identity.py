"""Usuarios con nombre de usuario de WhatsApp (BSUID), reintentos, estados de entrega y caché del agente."""

from types import SimpleNamespace

import httpx
import pytest

from app.db.models import contact_handle, has_phone
from app.services import conversation_service
from app.whatsapp import client as wa_client
from app.whatsapp.client import WhatsAppClient, recipient_fields
from app.whatsapp.parser import parse_incoming_messages, parse_statuses

BSUID = "CO.1090028550326215"


def _messages_payload(contacts: list[dict], messages: list[dict]) -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {"id": "1", "changes": [{"field": "messages", "value": {"contacts": contacts, "messages": messages}}]}
        ],
    }


def test_message_without_phone_uses_bsuid_as_sender() -> None:
    # Formato de Meta cuando el usuario adoptó nombre de usuario y no comparte su número.
    payload = _messages_payload(
        [{"profile": {"name": "Sheena Nelson", "username": "realsheenanelson"}, "user_id": BSUID}],
        [{"from_user_id": BSUID, "id": "wamid.1", "timestamp": "1", "type": "text", "text": {"body": "hola"}}],
    )

    [incoming] = parse_incoming_messages(payload)

    assert incoming.from_wa_id == BSUID
    assert incoming.phone is None
    assert incoming.user_id == BSUID
    assert incoming.username == "realsheenanelson"
    assert incoming.profile_name == "Sheena Nelson"
    assert incoming.text == "hola"


def test_message_with_phone_keeps_phone_and_bsuid() -> None:
    payload = _messages_payload(
        [{"profile": {"name": "Ana"}, "wa_id": "573001112233", "user_id": BSUID}],
        [{"from": "573001112233", "from_user_id": BSUID, "id": "wamid.2", "type": "text", "text": {"body": "hola"}}],
    )

    [incoming] = parse_incoming_messages(payload)

    assert incoming.from_wa_id == "573001112233"
    assert incoming.user_id == BSUID
    assert incoming.profile_name == "Ana"


def test_template_button_and_location_are_understood() -> None:
    payload = _messages_payload(
        [{"profile": {"name": "Ana"}, "wa_id": "573001112233"}],
        [
            {"from": "573001112233", "id": "w1", "type": "button", "button": {"text": "Ya pagué", "payload": "paid"}},
            {
                "from": "573001112233",
                "id": "w2",
                "type": "location",
                "location": {"latitude": 4.6, "longitude": -74.1, "name": "Sede Castilla"},
            },
        ],
    )

    button, location = parse_incoming_messages(payload)

    assert button.text == "Ya pagué"
    assert button.interactive_reply_id == "paid"
    assert "Sede Castilla" in location.text


def test_recipient_fields_switch_to_recipient_for_bsuid() -> None:
    assert recipient_fields("573001112233") == {"to": "573001112233"}
    assert recipient_fields(BSUID) == {"recipient_type": "individual", "recipient": BSUID}


@pytest.mark.asyncio
async def test_send_text_to_bsuid_uses_recipient(monkeypatch) -> None:
    sent: dict = {}

    async def fake_post(self, path, json=None, files=None, data=None):
        sent.update(json)
        return {"messages": [{"id": "wamid.out"}]}

    monkeypatch.setattr(WhatsAppClient, "_post", fake_post)
    await WhatsAppClient().send_text(to=BSUID, body="Hola")

    assert sent["recipient"] == BSUID
    assert "to" not in sent


@pytest.mark.asyncio
async def test_message_without_recipient_fails_fast_without_calling_meta(monkeypatch) -> None:
    calls = 0

    def fake_http_client():
        nonlocal calls
        calls += 1
        raise AssertionError("no debería llamar a Meta")

    monkeypatch.setattr(wa_client, "_http_client", fake_http_client)
    with pytest.raises(ValueError):
        await WhatsAppClient().send_text(to="", body="Hola")
    assert calls == 0


def test_only_transient_errors_are_retried() -> None:
    request = httpx.Request("POST", "https://graph.facebook.com")

    def status_error(code: int) -> httpx.HTTPStatusError:
        return httpx.HTTPStatusError("x", request=request, response=httpx.Response(code, request=request))

    assert not wa_client._is_transient(status_error(400))
    assert wa_client._is_transient(status_error(429))
    assert wa_client._is_transient(status_error(503))
    assert wa_client._is_transient(httpx.ConnectError("x"))


def test_contact_handle_for_staff() -> None:
    assert contact_handle(SimpleNamespace(wa_id="573001112233")) == "+573001112233"
    assert contact_handle(SimpleNamespace(wa_id=BSUID, username="ana.praxis")) == "@ana.praxis"
    assert contact_handle(SimpleNamespace(wa_id=BSUID, username=None)) == BSUID
    assert has_phone(SimpleNamespace(wa_id="573001112233"))
    assert not has_phone(SimpleNamespace(wa_id=BSUID))


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _StatusSession:
    def __init__(self, messages: dict):
        self._messages = messages
        self._pending: list[str] = []

    async def execute(self, stmt):
        wamid = stmt.compile(compile_kwargs={"literal_binds": True}).string.rsplit("'", 2)[-2]
        return _Result(self._messages.get(wamid))

    async def flush(self):
        pass


@pytest.mark.asyncio
async def test_delivery_status_never_goes_backwards_and_keeps_errors() -> None:
    read_msg = SimpleNamespace(delivery_status="read", delivery_error=None)
    sent_msg = SimpleNamespace(delivery_status="sent", delivery_error=None)
    session = _StatusSession({"wamid.read": read_msg, "wamid.sent": sent_msg})
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "statuses": [
                                {"id": "wamid.read", "status": "delivered", "recipient_user_id": BSUID},
                                {
                                    "id": "wamid.sent",
                                    "status": "failed",
                                    "recipient_id": "573001112233",
                                    "errors": [{"code": 131047, "title": "Re-engagement message"}],
                                },
                            ]
                        }
                    }
                ]
            }
        ]
    }

    statuses = parse_statuses(payload)
    updated = await conversation_service.update_delivery_statuses(session, statuses)

    assert statuses[0].recipient_id == BSUID
    assert updated == 2
    assert read_msg.delivery_status == "read"
    assert sent_msg.delivery_status == "failed"
    assert "131047" in sent_msg.delivery_error


@pytest.mark.asyncio
async def test_agent_caches_fixed_prompt_and_sends_attachments(monkeypatch) -> None:
    from app.agent import claude_agent

    captured: dict = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text="Veo tu captura.")],
            usage=SimpleNamespace(
                input_tokens=10, cache_read_input_tokens=6600, cache_creation_input_tokens=0, output_tokens=5
            ),
        )

    monkeypatch.setattr(claude_agent._client.messages, "create", fake_create)
    contact = SimpleNamespace(wa_id=BSUID, is_new=False, profile_name="Ana")
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}

    reply, _ = await claude_agent.run_agent_turn(
        "(te envió una imagen sin texto)", [], SimpleNamespace(contact=contact), attachments=[image]
    )

    assert reply == "Veo tu captura."
    assert captured["cache_control"] == {"type": "ephemeral"}
    fixed, per_contact = captured["system"]
    assert fixed["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in per_contact
    assert "celular" in per_contact["text"]  # sin número: el agente debe pedirlo
    assert captured["messages"][-1]["content"][0] == image
