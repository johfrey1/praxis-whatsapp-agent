"""Menú interactivo principal (WhatsApp interactive list)."""

MENU_TRIGGER_WORDS = {"menu", "menú", "hola", "inicio", "start"}

MAIN_MENU_SECTIONS = [
    {
        "title": "Información y matrícula",
        "rows": [
            {"id": "menu_cursos", "title": "📚 Cursos y niveles", "description": "Conoce nuestros programas"},
            {"id": "menu_horarios", "title": "🕒 Horarios", "description": "Horarios de clases disponibles"},
            {"id": "menu_precios", "title": "💲 Precios y documentos", "description": "Precios y material informativo"},
        ],
    },
    {
        "title": "Ya soy estudiante",
        "rows": [
            {"id": "menu_consulta", "title": "🙋 Tengo una consulta", "description": "Soporte para estudiantes actuales"},
        ],
    },
    {
        "title": "Pagos y cartera",
        "rows": [
            {"id": "menu_pagar_cuota", "title": "💰 Pagar cuota en línea", "description": "Paga ya con Nequi, PSE o tarjeta"},
            {"id": "menu_pagos", "title": "💳 Pagos", "description": "Consulta sobre pagos"},
            {"id": "menu_cartera", "title": "📋 Cartera", "description": "Aclaraciones de cartera"},
            {"id": "menu_paz_salvo", "title": "✅ Paz y salvo", "description": "Solicitar paz y salvo"},
            {"id": "menu_recibo", "title": "🧾 Subir recibo de pago", "description": "Envía tu comprobante de pago"},
        ],
    },
]


async def send_main_menu(whatsapp_client, to: str) -> None:
    await whatsapp_client.send_interactive_list(
        to=to,
        header="Praxis English School",
        body="¡Hola! Soy Antony, el asistente virtual de Praxis. ¿En qué te puedo ayudar hoy?",
        button_text="Ver opciones",
        sections=MAIN_MENU_SECTIONS,
    )


MEDIA_FALLBACK_TEXT = {
    "audio": (
        "Recibí tu nota de voz 🎧 Por ahora no puedo escucharla directamente. "
        "¿Me escribes tu pregunta en texto? Si prefieres, escribe *asesor* para hablar con una persona."
    ),
    "image": (
        "Recibí tu imagen 📷 Por ahora no puedo verla directamente. "
        "¿Me cuentas en texto qué necesitas? Si prefieres, escribe *asesor* para hablar con una persona."
    ),
    "document": (
        "Recibí tu documento 📄 Un asesor lo va a revisar. Mientras tanto, ¿en qué más te puedo ayudar?"
    ),
}
