from anthropic import AsyncAnthropic

from app.agent.prompts import SYSTEM_PROMPT, contact_new_context
from app.agent.text import strip_emojis
from app.agent.tools import TOOL_DEFINITIONS, ToolContext, execute_tool
from app.config import get_settings
from app.db.models import Message, MessageDirection, has_phone
from app.logging_config import get_logger

settings = get_settings()
logger = get_logger(__name__)

_client = AsyncAnthropic(api_key=settings.anthropic_api_key)

_MAX_TOOL_ITERATIONS = 5


def _tool_note(msg: Message) -> str:
    """Resumen de las tools que se ejecutaron en un turno anterior. Sin él, el modelo no sabía que
    ya había guardado al prospecto: lo guardaba otra vez y le decía al cliente que antes no quedó."""
    calls = (msg.tool_calls or {}).get("calls") or []
    done = []
    for call in calls:
        result = call.get("result")
        outcome = (result.get("error") or result.get("status")) if isinstance(result, dict) else None
        done.append(f"{call.get('tool')} → {outcome or 'ok'}")
    return f"[Nota interna, no visible para el cliente: en este turno ejecutaste {'; '.join(done)}]\n" if done else ""


def _history_to_messages(history: list[Message]) -> list[dict]:
    """Convierte el historial persistido a mensajes Anthropic. Se guarda solo el texto final de cada
    turno (no los bloques de tool_use intermedios) para mantener el prompt compacto, con una nota de
    las tools que se ejecutaron y su resultado."""
    messages = []
    for msg in history:
        if not msg.content:
            continue
        if msg.direction == MessageDirection.inbound:
            messages.append({"role": "user", "content": msg.content})
        else:
            messages.append({"role": "assistant", "content": _tool_note(msg) + msg.content})
    return messages


async def run_agent_turn(
    user_text: str,
    history: list[Message],
    tool_ctx: ToolContext,
    attachments: list[dict] | None = None,
) -> tuple[str, list[dict]]:
    """Ejecuta el loop de tool-use de Claude para un turno de conversación.

    `attachments` son bloques de imagen o PDF que el usuario envió con este mensaje.
    Devuelve (texto_final_para_el_usuario, registro_de_tool_calls_ejecutadas).
    """
    messages = _history_to_messages(history)
    if attachments:
        messages.append({"role": "user", "content": [*attachments, {"type": "text", "text": user_text}]})
    else:
        messages.append({"role": "user", "content": user_text})

    contact = tool_ctx.contact
    # El prompt fijo (tools + SYSTEM_PROMPT, ~6.600 tokens) va primero y en caché; el contexto del
    # contacto cambia por persona y va después del punto de caché para no invalidarlo.
    system_blocks = [
        {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": contact_new_context(contact.is_new, contact.profile_name, has_phone(contact))},
    ]
    executed_tool_calls: list[dict] = []

    for _ in range(_MAX_TOOL_ITERATIONS):
        response = await _client.messages.create(
            model=settings.claude_model,
            max_tokens=settings.claude_max_tokens,
            system=system_blocks,
            tools=TOOL_DEFINITIONS,
            messages=messages,
            # Caché automática de la conversación: cada vuelta del loop de tools reutiliza la anterior.
            cache_control={"type": "ephemeral"},
        )
        usage = response.usage
        logger.info(
            "claude_usage",
            wa_id=contact.wa_id,
            stop_reason=response.stop_reason,
            input_tokens=usage.input_tokens,
            cache_read=usage.cache_read_input_tokens,
            cache_write=usage.cache_creation_input_tokens,
            output_tokens=usage.output_tokens,
        )
        if response.stop_reason == "max_tokens":
            logger.warning("claude_max_tokens_reached", wa_id=contact.wa_id, max_tokens=settings.claude_max_tokens)

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
    return strip_emojis("\n".join(parts)) or "¿Podrías repetir tu mensaje? No logré procesarlo."


def _stringify(result: dict) -> str:
    import json

    return json.dumps(result, ensure_ascii=False, default=str)
