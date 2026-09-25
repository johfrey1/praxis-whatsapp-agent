from anthropic import AsyncAnthropic

from app.agent.prompts import SYSTEM_PROMPT, contact_new_context
from app.agent.tools import TOOL_DEFINITIONS, ToolContext, execute_tool
from app.config import get_settings
from app.db.models import Message, MessageDirection
from app.logging_config import get_logger

settings = get_settings()
logger = get_logger(__name__)

_client = AsyncAnthropic(api_key=settings.anthropic_api_key)

_MAX_TOOL_ITERATIONS = 5


def _history_to_messages(history: list[Message]) -> list[dict]:
    """Convierte el historial persistido a mensajes Anthropic. Se guarda solo el texto final
    de cada turno (no los bloques de tool_use intermedios) para mantener el prompt compacto;
    el resultado de las tools ya quedó reflejado en la respuesta de texto que se guardó."""
    messages = []
    for msg in history:
        if not msg.content:
            continue
        role = "user" if msg.direction == MessageDirection.inbound else "assistant"
        messages.append({"role": role, "content": msg.content})
    return messages


async def run_agent_turn(
    user_text: str,
    history: list[Message],
    tool_ctx: ToolContext,
) -> tuple[str, list[dict]]:
    """Ejecuta el loop de tool-use de Claude para un turno de conversación.

    Devuelve (texto_final_para_el_usuario, registro_de_tool_calls_ejecutadas).
    """
    messages = _history_to_messages(history)
    messages.append({"role": "user", "content": user_text})

    system_prompt = f"{SYSTEM_PROMPT}\n\n{contact_new_context(tool_ctx.contact.is_new)}"
    executed_tool_calls: list[dict] = []

    for _ in range(_MAX_TOOL_ITERATIONS):
        response = await _client.messages.create(
            model=settings.claude_model,
            max_tokens=settings.claude_max_tokens,
            system=system_prompt,
            tools=TOOL_DEFINITIONS,
            messages=messages,
        )

        if response.stop_reason != "tool_use":
            return _extract_text(response), executed_tool_calls

        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            logger.info("tool_call", tool=block.name, input=block.input, wa_id=tool_ctx.contact.wa_id)
            try:
                result = await execute_tool(block.name, block.input, tool_ctx)
            except Exception:
                # Si una integración falla (Strapi, Wompi, Meta), Claude debe poder responder igual
                # en vez de que el usuario se quede sin respuesta.
                logger.exception("tool_failed", tool=block.name, wa_id=tool_ctx.contact.wa_id)
                result = {"error": "tool_failed"}
            executed_tool_calls.append({"tool": block.name, "input": block.input, "result": result})
            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": _stringify(result)})

        messages.append({"role": "user", "content": tool_results})

    logger.warning("max_tool_iterations_reached", wa_id=tool_ctx.contact.wa_id)
    return (
        "Estoy verificando esa información, dame un momento y un asesor te confirma en breve.",
        executed_tool_calls,
    )


def _extract_text(response) -> str:
    parts = [block.text for block in response.content if block.type == "text"]
    return "\n".join(parts).strip() or "¿Podrías repetir tu mensaje? No logré procesarlo."


def _stringify(result: dict) -> str:
    import json

    return json.dumps(result, ensure_ascii=False, default=str)
