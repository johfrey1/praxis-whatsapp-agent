"""El bot debe sonar como una persona: sin emojis en nada que lea un estudiante o prospecto."""

import pytest

from app.agent.menu import MAIN_MENU_SECTIONS, MEDIA_FALLBACK_TEXT
from app.agent.prompts import SYSTEM_PROMPT
from app.agent.text import strip_emojis
from app.agent.welcome import build_welcome


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("¡Hola, Ana! 😊 Qué gusto", "¡Hola, Ana! Qué gusto"),
        ("Listo 👍", "Listo"),
        ("Viajar ✈️, trabajo 💼 y estudio 🎓", "Viajar, trabajo y estudio"),
        ("Primera línea 🙌\nSegunda línea", "Primera línea\nSegunda línea"),
        ("Sin emojis, ¿ves? Acentos: años, canción.", "Sin emojis, ¿ves? Acentos: años, canción."),
        ("👋", ""),
    ],
)
def test_strip_emojis(raw, expected):
    assert strip_emojis(raw) == expected


def _has_emoji(text: str) -> bool:
    return strip_emojis(text) != text.strip()


def test_welcome_without_emojis():
    assert not _has_emoji(build_welcome("Ana Gómez"))
    assert not _has_emoji(build_welcome(None))


def test_menu_without_emojis():
    for section in MAIN_MENU_SECTIONS:
        for row in section["rows"]:
            assert not _has_emoji(row["title"]), row["id"]
            assert not _has_emoji(row["description"]), row["id"]


def test_media_fallbacks_without_emojis():
    for text in MEDIA_FALLBACK_TEXT.values():
        assert not _has_emoji(text)


def test_menu_titles_fit_whatsapp_limit():
    for section in MAIN_MENU_SECTIONS:
        for row in section["rows"]:
            assert len(row["title"]) <= 24, row["title"]


def test_prompt_forbids_emojis():
    assert "sin emojis" in SYSTEM_PROMPT
