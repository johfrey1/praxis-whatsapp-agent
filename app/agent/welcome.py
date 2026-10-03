"""Bienvenida para contactos nuevos: texto fijo (instantáneo y siempre igual), personalizado con
el nombre del perfil de WhatsApp. Se enfoca en lo que la persona quiere lograr con el inglés."""

import re
import unicodedata

_GREETING_WORDS = {
    "hola", "holi", "holaa", "hla", "ola", "buenas", "buenos dias", "buen dia", "buenas tardes",
    "buenas noches", "hey", "hi", "hello", "info", "informacion", "saludos", "que tal",
}

# Opciones del menú que indican interés en inscribirse (prospecto).
PROSPECT_MENU_IDS = {"menu_cursos", "menu_horarios", "menu_precios"}


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z ]", "", text).strip()


def is_plain_greeting(text: str | None) -> bool:
    """True si el mensaje es solo un saludo ("Hola", "buenas tardes", "info")."""
    if not text:
        return True
    normalized = re.sub(r"\s+", " ", _normalize(text))
    if normalized in _GREETING_WORDS:
        return True
    # "hola buenas", "holaaa", "hola como estas"
    words = normalized.split()
    return 0 < len(words) <= 3 and words[0].startswith(("hol", "buen", "hey"))


def first_name(profile_name: str | None) -> str | None:
    """Primer nombre legible del perfil de WhatsApp, o None si no parece un nombre."""
    if not profile_name:
        return None
    for token in profile_name.split():
        cleaned = "".join(ch for ch in token if ch.isalpha())
        if len(cleaned) >= 2 and cleaned == token.strip(".,"):
            return cleaned.capitalize()
    return None


def build_welcome(profile_name: str | None) -> str:
    name = first_name(profile_name)
    hello = f"Hola, {name}." if name else "Hola."
    return (
        f"{hello} Gracias por escribirnos. Soy Antony, de *Praxis English School*.\n\n"
        "Cuéntame, ¿qué te gustaría lograr con el inglés? Por ejemplo viajar, crecer en tu trabajo, "
        "estudiar o simplemente hablar con más confianza.\n\n"
        "Y si ya eres estudiante, dime en qué te puedo ayudar."
    )
