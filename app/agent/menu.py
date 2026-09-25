"""Menú interactivo principal (WhatsApp interactive list)."""

MENU_TRIGGER_WORDS = {"menu", "menú", "hola", "inicio", "start"}

MAIN_MENU_SECTIONS = [
    {
        "title": "Praxis English School",
        "rows": [
            {"id": "menu_cursos", "title": "📚 Cursos y niveles", "description": "Conoce nuestros programas"},
            {"id": "menu_horarios", "title": "🕒 Horarios y precios", "description": "Consulta disponibilidad"},
            {"id": "menu_inscripcion", "title": "📝 Quiero inscribirme", "description": "Deja tus datos, te contactamos"},
            {"id": "menu_pagar_cuota", "title": "💳 Pagar cuota", "description": "Paga en línea con Wompi"},
            {"id": "menu_documentos", "title": "📄 Solicitar documentos", "description": "Brochure, temarios, etc."},
            {"id": "menu_asesor", "title": "🙋 Hablar con un asesor", "description": "Atención humana"},
        ],
    }
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
