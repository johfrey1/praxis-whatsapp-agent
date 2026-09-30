from app.agent.welcome import build_welcome, first_name, is_plain_greeting
from app.db.models import Lead
from app.services.lead_service import format_lead_notification, is_valid_email


def test_plain_greetings():
    for text in ["Hola", "hola!!", "Buenas tardes 👋", "holaaa", "Hola buenas", "info", "  HOLA  ", None, ""]:
        assert is_plain_greeting(text), text


def test_not_plain_greetings():
    for text in ["Quiero aprender inglés para viajar", "cuánto vale el curso", "hola, quiero pagar mi cuota ya mismo"]:
        assert not is_plain_greeting(text), text


def test_first_name():
    assert first_name("ana maría gómez") == "Ana"
    assert first_name("🌸 Laura") == "Laura"
    assert first_name("J") is None
    assert first_name("+57 310 000") is None
    assert first_name(None) is None


def test_welcome_is_personal_and_does_not_mention_schedules():
    text = build_welcome("Ana Gómez")
    assert text.startswith("¡Hola, Ana!")
    assert "qué te gustaría lograr con el inglés" in text
    assert "horario" not in text.lower()
    assert build_welcome(None).startswith("¡Hola! 😊")


def test_email_validation():
    assert is_valid_email("ana@gmail.com")
    assert is_valid_email(" ana.gomez@empresa.com.co ")
    assert not is_valid_email("ana@gmail")
    assert not is_valid_email("ana gmail.com")


def test_lead_notification_has_everything_to_call():
    lead = Lead(full_name="Ana Gómez", email="ana@gmail.com", phone="573100000000", program_interest="viajar a Canadá")
    text = format_lead_notification(lead)
    assert "Ana Gómez" in text and "ana@gmail.com" in text and "+573100000000" in text and "viajar a Canadá" in text
