"""Limpieza de texto saliente hacia estudiantes y prospectos."""

import re

# Pictogramas, emoticonos, banderas y selectores de variación. No toca letras acentuadas,
# ¿ ¡ ni signos de puntuación.
_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U00002B00-\U00002BFF"
    "\U00002300-\U000023FF"
    "\U0000FE00-\U0000FE0F"
    "\U0000200D\U000020E3"
    "\U0001F1E6-\U0001F1FF"
    "]+"
)


def strip_emojis(text: str) -> str:
    """Quita emojis y deja el texto con espacios y saltos de línea limpios."""
    cleaned = _EMOJI_RE.sub("", text)
    cleaned = re.sub(r"[ \t]+([.,;:!?])", r"\1", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r" *\n *", "\n", cleaned)
    return cleaned.strip()
