from types import SimpleNamespace

import pytest

from app.services import conversation_service

CONTACT = SimpleNamespace(wa_id="573012042870", profile_name="Juan")


class FakeWhatsApp:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_staff_message(self, to: str, text: str) -> None:
        self.sent.append(to)


@pytest.fixture(autouse=True)
def numbers(monkeypatch):
    s = conversation_service.settings
    monkeypatch.setattr(s, "staff_notification_numbers", "573001110000,573001110001")
    monkeypatch.setattr(s, "student_request_numbers", "573102394548")


@pytest.mark.asyncio
async def test_prospect_alerts_go_only_to_advisors() -> None:
    wa = FakeWhatsApp()
    await conversation_service.notify_staff(wa, CONTACT, "Nuevo prospecto")
    assert wa.sent == ["573001110000", "573001110001"]


@pytest.mark.asyncio
async def test_student_alerts_go_only_to_student_service_number() -> None:
    wa = FakeWhatsApp()
    await conversation_service.notify_staff(wa, CONTACT, "Conversación escalada", student=True)
    assert wa.sent == ["573102394548"]


@pytest.mark.asyncio
async def test_escalation_tool_routes_by_is_student(monkeypatch) -> None:
    from app.agent import tools

    async def fake_escalate(session, conversation, contact):
        return None

    monkeypatch.setattr(conversation_service, "escalate_conversation", fake_escalate)
    for is_student, expected in ((True, ["573102394548"]), (False, ["573001110000", "573001110001"])):
        wa = FakeWhatsApp()
        ctx = SimpleNamespace(session=None, conversation=None, contact=CONTACT, whatsapp_client=wa)
        result = await tools.execute_tool("escalate_to_human", {"reason": "queja", "is_student": is_student}, ctx)
        assert result == {"status": "escalated"}
        assert wa.sent == expected


def test_escalate_tool_requires_is_student() -> None:
    from app.agent.tools import TOOL_DEFINITIONS

    tool = next(t for t in TOOL_DEFINITIONS if t["name"] == "escalate_to_human")
    assert "is_student" in tool["input_schema"]["required"]
