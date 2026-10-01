import os

os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("WHATSAPP_PHONE_NUMBER_ID", "test")
os.environ.setdefault("WHATSAPP_ACCESS_TOKEN", "test")
os.environ.setdefault("WHATSAPP_APP_SECRET", "test")
os.environ.setdefault("WHATSAPP_VERIFY_TOKEN", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("STRAPI_BASE_URL", "http://localhost:1337")
os.environ.setdefault("ADMIN_API_KEY", "test")

from types import SimpleNamespace

import pytest

from app.services import student_request_service as srs

VALID = {
    "request_type": "paz_y_salvo",
    "national_id": "1.020.304.050",
    "phone": "310 239 4548",
    "email": "ana@example.com",
    "request": "Necesito el paz y salvo del contrato 12345",
    "full_name": "Ana Pérez",
}


def test_build_request_normalizes_fields() -> None:
    req = srs.build_request(VALID)
    assert req.national_id == "1020304050"
    assert req.phone == "573102394548"
    assert req.request_type == "paz_y_salvo"


@pytest.mark.parametrize(
    ("field", "value"),
    [("national_id", "12"), ("phone", "123"), ("email", "ana@"), ("request", "  ")],
)
def test_build_request_rejects_invalid(field: str, value: str) -> None:
    with pytest.raises(srs.StudentRequestValidationError, match=field):
        srs.build_request({**VALID, field: value})


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("3102394548", "573102394548"),
        ("+57 310 239 4548", "573102394548"),
        ("6044445566", "576044445566"),
        ("+1 305 555 0100", "13055550100"),
        ("32324434433", None),  # 11 dígitos: no es celular colombiano ni Bélgica
        ("2343v", None),
        ("57310239454", None),
    ],
)
def test_normalize_phone(raw: str, expected: str | None) -> None:
    assert srs.normalize_phone(raw) == expected


def test_unknown_request_type_falls_back_to_otro() -> None:
    assert srs.build_request({**VALID, "request_type": "xyz"}).request_type == "otro"


class FakeWhatsApp:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_text(self, to: str, body: str, preview_url: bool = False) -> dict:
        self.sent.append((to, body))
        return {}

    async def send_staff_message(self, to: str, text: str) -> dict:
        return await self.send_text(to=to, body=text)


@pytest.mark.asyncio
async def test_send_request_goes_to_student_request_number(monkeypatch) -> None:
    monkeypatch.setattr(srs.settings, "student_request_numbers", "+573102394548")
    wa = FakeWhatsApp()
    contact = SimpleNamespace(wa_id="573001112233", profile_name="Ana")

    await srs.send_request(wa, srs.build_request(VALID), contact)

    assert len(wa.sent) == 1
    to, body = wa.sent[0]
    assert to == "573102394548"
    for expected in ("1020304050", "+573102394548", "ana@example.com", "paz y salvo del contrato 12345", "Paz y salvo"):
        assert expected in body
