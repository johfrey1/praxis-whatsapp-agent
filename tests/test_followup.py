from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.db.models import MessageDirection
from app.services import followup_service

NOW = datetime(2026, 10, 21, 20, 0, tzinfo=timezone.utc)
IDLE = timedelta(minutes=30)


def msg(minutes_ago: int, direction: str, content: str | None = None, tools: list[str] | None = None):
    return SimpleNamespace(
        created_at=NOW - timedelta(minutes=minutes_ago),
        direction=MessageDirection(direction),
        content=content,
        tool_calls={"calls": [{"tool": t} for t in tools]} if tools else None,
    )


def test_client_who_stops_answering_is_handed_off() -> None:
    messages = [msg(50, "inbound", "Hola, cuánto vale el curso?"), msg(49, "outbound", "Cuesta... ¿Me das tu nombre?")]
    assert followup_service.needs_followup(messages, NOW, IDLE) is True


def test_conversation_still_going_is_left_alone() -> None:
    messages = [msg(10, "inbound", "Hola"), msg(9, "outbound", "¡Hola! ¿En qué te ayudo?")]
    assert followup_service.needs_followup(messages, NOW, IDLE) is False


def test_finished_conversations_are_not_handed_off() -> None:
    for tool in ("save_lead", "escalate_to_human", "create_installment_payment_link", "submit_student_request"):
        messages = [msg(60, "inbound", "Quiero inscribirme"), msg(59, "outbound", "Listo", tools=[tool])]
        assert followup_service.needs_followup(messages, NOW, IDLE) is False, tool


def test_only_bot_messages_are_ignored() -> None:
    assert followup_service.needs_followup([msg(60, "outbound", "Recordatorio")], NOW, IDLE) is False


def test_summary_tells_staff_what_the_client_asked() -> None:
    messages = [
        msg(50, "inbound", "Hola"),
        msg(49, "outbound", "¡Hola!"),
        msg(48, "inbound", "Horarios de sábado?"),
        msg(47, "outbound", "Sábados de 8 a 12. ¿Te inscribo?"),
    ]
    summary = followup_service.build_summary(messages, IDLE)
    assert "30 min" in summary
    assert "Hola / Horarios de sábado?" in summary
    assert "Sábados de 8 a 12" in summary


def test_summary_flags_unanswered_last_message() -> None:
    summary = followup_service.build_summary([msg(50, "inbound", "Hola?")], IDLE)
    assert "sin respuesta" in summary


def test_failed_save_lead_does_not_finish_the_conversation() -> None:
    messages = [msg(60, "inbound", "Soy Maria, maria@x.com")]
    messages.append(msg(59, "outbound", "¿Tu celular?"))
    messages[-1].tool_calls = {"calls": [{"tool": "save_lead", "result": {"error": "phone_required"}}]}
    assert followup_service.needs_followup(messages, NOW, IDLE) is True
