import os

os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("WHATSAPP_PHONE_NUMBER_ID", "test")
os.environ.setdefault("WHATSAPP_ACCESS_TOKEN", "test")
os.environ.setdefault("WHATSAPP_APP_SECRET", "test")
os.environ.setdefault("WHATSAPP_VERIFY_TOKEN", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("STRAPI_BASE_URL", "http://localhost:1337")
os.environ.setdefault("ADMIN_API_KEY", "test")

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from app.agent import tools
from app.agent.menu import MAIN_MENU_SECTIONS
from app.db.models import PaymentRequest, PaymentStatus
from app.services import payment_service, student_request_service
from app.wompi.client import WompiClient

CONTACT = SimpleNamespace(id="c1", wa_id="573012042870", profile_name="Juan")


def _approved_service_payment() -> PaymentRequest:
    now = datetime.now(timezone.utc)
    return PaymentRequest(
        reference="PRX-1",
        national_id="1020304050",
        contract_number="12345",
        account_number=None,
        concept="certificado",
        student_name="Ana María Pérez",
        status=PaymentStatus.approved,
        amount_in_cents=1800000,
        payment_method_type="NEQUI",
        wompi_transaction_id="tx-1",
        paid_at=now,
        expires_at=now + timedelta(hours=1),
        payment_url="https://checkout.wompi.co/l/x",
        wompi_payment_link_id="x",
    )


def test_normalize_service_data_accepts_full_names() -> None:
    assert payment_service.normalize_service_data("1.020.304.050", "prx-12", "  ana   maría  pérez ") == (
        "1020304050",
        "PRX-12",
        "ana maría pérez",
    )


@pytest.mark.parametrize("name", ["", "Ana", "Ana 123 Pérez", "x" * 130])
def test_normalize_service_data_rejects_incomplete_names(name) -> None:
    with pytest.raises(payment_service.PaymentValidationError):
        payment_service.normalize_service_data("1020304050", "12345", name)


@pytest.mark.asyncio
async def test_wompi_fixed_amount_sends_amount_and_currency_together(monkeypatch) -> None:
    captured = {}

    async def fake_post(self, url, headers=None, json=None, **kwargs):
        captured["json"] = json
        return httpx.Response(200, json={"data": {"id": "L1"}}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    await WompiClient().create_payment_link(
        name="n", description="d", sku="s", expires_at=datetime(2026, 10, 4, tzinfo=timezone.utc), amount_in_cents=1800000
    )
    assert captured["json"]["amount_in_cents"] == 1800000
    assert captured["json"]["currency"] == "COP"


def test_requirement_has_everything_the_manager_needs() -> None:
    text = payment_service.format_service_requirement(_approved_service_payment(), CONTACT)
    for expected in ("certificado", "Ana María Pérez", "1020304050", "12345", "+573012042870", "$18.000 COP", "PRX-1", "tx-1"):
        assert expected in text, expected


def test_student_copy_contains_the_requirement() -> None:
    text = payment_service._result_message(_approved_service_payment(), CONTACT)
    assert "gestor de estudiantes" in text
    assert "Ana María Pérez" in text and "PRX-1" in text


class FakeWhatsApp:
    def __init__(self, fail_student_text: bool = False) -> None:
        self.staff: list[str] = []
        self.texts: list[str] = []
        self.fail_student_text = fail_student_text

    async def send_staff_message(self, to, text):
        self.staff.append(to)

    async def send_text(self, to, body):
        if self.fail_student_text:
            raise RuntimeError("meta down")
        self.texts.append(to)


class FakeSession:
    def __init__(self, contact):
        self.contact = contact

    async def get(self, model, key):
        return self.contact if key == self.contact.id else None


@pytest.mark.asyncio
async def test_approved_service_payment_reaches_manager_even_if_student_message_fails(monkeypatch) -> None:
    monkeypatch.setattr(payment_service.settings, "student_manager_numbers", "573001234567")
    payment = _approved_service_payment()
    payment.contact_id = "c1"
    payment.conversation_id = None
    wa = FakeWhatsApp(fail_student_text=True)

    with pytest.raises(RuntimeError):
        await payment_service.notify_payment_result(FakeSession(CONTACT), wa, payment)

    assert wa.staff == ["573001234567"]


@pytest.mark.asyncio
async def test_approved_service_payment_copies_manager_and_payer(monkeypatch) -> None:
    monkeypatch.setattr(payment_service.settings, "student_manager_numbers", "573001234567")
    payment = _approved_service_payment()
    payment.contact_id = "c1"
    payment.conversation_id = None
    wa = FakeWhatsApp()

    await payment_service.notify_payment_result(FakeSession(CONTACT), wa, payment)

    assert wa.staff == ["573001234567"]
    assert wa.texts == ["573012042870"]


def test_manager_numbers_default_to_student_service_number(monkeypatch) -> None:
    s = payment_service.settings
    monkeypatch.setattr(s, "student_manager_numbers", "")
    monkeypatch.setattr(s, "student_request_numbers", "573102394548")
    assert s.student_manager_numbers_list == ["573102394548"]


def test_new_request_types_available() -> None:
    for t in ("sabana_notas", "paz_y_salvo", "congelamiento", "extension_contrato", "retoma", "queja_reclamo_sugerencia", "reporte_datacredito"):
        assert t in student_request_service.REQUEST_TYPES
        req = student_request_service.build_request(
            {"request_type": t, "national_id": "1020304050", "phone": "3012042870", "email": "a@b.co", "request": "x"}
        )
        assert req.request_type == t


def test_service_tool_is_declared_with_required_fields() -> None:
    tool = next(t for t in tools.TOOL_DEFINITIONS if t["name"] == "create_service_payment_link")
    assert set(tool["input_schema"]["required"]) == {"concept", "national_id", "contract_number", "full_name"}
    assert tool["input_schema"]["properties"]["concept"]["enum"] == ["certificado"]


def test_only_certificates_are_charged() -> None:
    assert list(payment_service.SERVICE_CONCEPTS) == ["certificado"]


@pytest.mark.asyncio
async def test_transcript_and_clearance_are_not_charged(monkeypatch) -> None:
    monkeypatch.setattr(type(tools.settings), "payments_enabled", property(lambda self: True))
    ctx = SimpleNamespace(session=None, wompi_client=None, contact=CONTACT, conversation=None, whatsapp_client=None)
    for concept in ("sabana_notas", "paz_y_salvo"):
        result = await tools.execute_tool(
            "create_service_payment_link",
            {"concept": concept, "national_id": "1020304050", "contract_number": "12345", "full_name": "Ana Pérez"},
            ctx,
        )
        assert result["error"] == "invalid_data", concept


@pytest.mark.asyncio
async def test_service_tool_returns_invalid_data_for_bad_name(monkeypatch) -> None:
    monkeypatch.setattr(type(tools.settings), "payments_enabled", property(lambda self: True))
    ctx = SimpleNamespace(session=None, wompi_client=None, contact=CONTACT, conversation=None, whatsapp_client=None)
    result = await tools.execute_tool(
        "create_service_payment_link",
        {"concept": "certificado", "national_id": "1020304050", "contract_number": "12345", "full_name": "Ana"},
        ctx,
    )
    assert result["error"] == "invalid_data"


def test_menu_fits_whatsapp_limits() -> None:
    rows = [r for section in MAIN_MENU_SECTIONS for r in section["rows"]]
    assert len(rows) <= 10
    assert all(len(r["title"]) <= 24 and len(r["description"]) <= 72 for r in rows)
