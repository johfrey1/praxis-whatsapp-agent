from types import SimpleNamespace

from app.agent.claude_agent import _history_to_messages
from app.db.models import MessageDirection


def _msg(direction: str, content: str, tool_calls=None):
    return SimpleNamespace(direction=MessageDirection(direction), content=content, tool_calls=tool_calls)


def test_history_tells_the_model_which_tools_already_ran() -> None:
    history = [
        _msg("inbound", "Luz Marina, luz@hotmail.com"),
        _msg(
            "outbound",
            "Listo, quedaste registrada.",
            {"calls": [{"tool": "save_lead", "input": {}, "result": {"status": "saved", "lead_id": "x"}}]},
        ),
    ]
    messages = _history_to_messages(history)
    assert messages[0] == {"role": "user", "content": "Luz Marina, luz@hotmail.com"}
    assert messages[1]["role"] == "assistant"
    assert "save_lead → saved" in messages[1]["content"]
    assert messages[1]["content"].endswith("Listo, quedaste registrada.")


def test_failed_tools_and_plain_replies() -> None:
    failed = _msg("outbound", "¿Tu celular?", {"calls": [{"tool": "save_lead", "result": {"error": "phone_required"}}]})
    assert "save_lead → phone_required" in _history_to_messages([failed])[0]["content"]
    assert _history_to_messages([_msg("outbound", "Hola")])[0]["content"] == "Hola"
