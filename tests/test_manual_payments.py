import os

os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("WHATSAPP_PHONE_NUMBER_ID", "test")
os.environ.setdefault("WHATSAPP_ACCESS_TOKEN", "test")
os.environ.setdefault("WHATSAPP_APP_SECRET", "test")
os.environ.setdefault("WHATSAPP_VERIFY_TOKEN", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("STRAPI_BASE_URL", "http://localhost:1337")
os.environ.setdefault("ADMIN_API_KEY", "test")

import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.agent import tools
from app.agent.prompts import SYSTEM_PROMPT
from app.db.base import get_session
from app.db.models import ManualPayment
from app.main import app
from app.routers import webhook
from app.services import manual_payment_service

CONTACT = SimpleNamespace(id=uuid.uuid4(), wa_id="573012042870", profile_name="Juan")


class _Result:
    def __init__(self, value, items=None):
        self._value = value
        self._items = items or []

    def scalars(self):
        return self

    def first(self):
        return self._value

    def all(self):
        return self._items


class FakeSession:
    def __init__(self, existing=None):
        self.existing = existing
        self.added = []

    async def execute(self, _stmt):
        return _Result(self.existing)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass


class FakeWhatsApp:
    def __init__(self, mime="image/jpeg", template_fails=False):
        self.mime = mime
        self.template_fails = template_fails
        self.texts, self.staff, self.templates, self.images, self.captions = [], [], [], [], []

    async def download_media(self, media_id):
        return b"fake-bytes", self.mime

    async def upload_media(self, data, filename, mime):
        return "uploaded-1"

    async def send_staff_message(self, to, text):
        self.staff.append((to, text))

    async def send_template(self, **kwargs):
        self.templates.append(kwargs)
        if self.template_fails:
            raise RuntimeError("132001")

    async def send_image_by_media_id(self, to, media_id, caption=None):
        self.images.append(to)
        self.captions.append(caption or "")

    async def send_text(self, to, body):
        self.texts.append((to, body))


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    s = manual_payment_service.settings
    monkeypatch.setattr(s, "document_storage_path", str(tmp_path))
    monkeypatch.setattr(s, "student_manager_numbers", "573102394548")
    monkeypatch.setattr(s, "payment_receipt_numbers", "")


@pytest.mark.asyncio
async def test_start_cuota_payment_normalizes_and_returns_instructions() -> None:
    session = FakeSession()
    payment = await manual_payment_service.start_payment(
        session, CONTACT, None, "cuota", "1.020.304.050", "prx-12", "001", amount_cop=150000
    )
    assert session.added == [payment]
    assert (payment.national_id, payment.contract_number, payment.account_number) == ("1020304050", "PRX-12", "001")
    assert payment.amount_in_cents == 15000000
    assert payment.status == "awaiting_proof"
    text = manual_payment_service.instructions(payment)
    assert "0089087583" in text and "PRAXIS SCHOOL" in text and payment.reference in text


@pytest.mark.asyncio
async def test_start_certificate_uses_fixed_fee_and_requires_full_name() -> None:
    payment = await manual_payment_service.start_payment(
        FakeSession(), CONTACT, None, "certificado", "1020304050", "12345", student_name="Ana María Pérez"
    )
    assert payment.amount_in_cents == 1800000
    with pytest.raises(manual_payment_service.ManualPaymentError):
        await manual_payment_service.start_payment(FakeSession(), CONTACT, None, "certificado", "1020304050", "12345", student_name="Ana")


@pytest.mark.asyncio
async def test_start_reuses_payment_awaiting_proof() -> None:
    existing = ManualPayment(reference="PRX-BOLD", concept="cuota", status="awaiting_proof")
    session = FakeSession(existing=existing)
    payment = await manual_payment_service.start_payment(session, CONTACT, None, "cuota", "1020304050", "12345", "001")
    assert payment is existing and session.added == []


@pytest.mark.asyncio
async def test_start_rejects_unknown_concept() -> None:
    with pytest.raises(manual_payment_service.ManualPaymentError):
        await manual_payment_service.start_payment(FakeSession(), CONTACT, None, "sabana_notas", "1020304050", "12345")


def _awaiting() -> ManualPayment:
    return ManualPayment(
        reference="PRX-B123",
        method="breb",
        concept="cuota",
        national_id="1020304050",
        contract_number="12345",
        account_number="001",
        student_name="Ana Pérez",
        amount_in_cents=15000000,
        status="awaiting_proof",
    )


@pytest.mark.asyncio
async def test_receive_proof_saves_evidence_and_forwards_to_manager(tmp_path) -> None:
    payment, wa = _awaiting(), FakeWhatsApp()
    await manual_payment_service.receive_proof(FakeSession(), wa, payment, CONTACT, "media-9")

    assert payment.status == "pending_approval"
    assert payment.proof_media_id == "media-9"
    assert (tmp_path / "proofs" / "PRX-B123.jpg").read_bytes() == b"fake-bytes"
    assert wa.staff[0][0] == "573102394548" and "PRX-B123" in wa.staff[0][1] and "Bre-B" in wa.staff[0][1]
    assert wa.templates[0]["name"] == "comprobante_transferencia"
    assert wa.templates[0]["header_image_media_id"] == "uploaded-1"
    assert wa.templates[0]["body_params"] == ["Bre-B", "Ana Pérez", "1020304050", "12345", "Cuota", "PRX-B123"]


@pytest.mark.asyncio
async def test_receive_proof_falls_back_to_free_image_when_template_fails() -> None:
    payment, wa = _awaiting(), FakeWhatsApp(template_fails=True)
    await manual_payment_service.receive_proof(FakeSession(), wa, payment, CONTACT, "media-9")
    assert wa.images == ["573012042870", "573102394548"]  # recibo al estudiante y captura al gestor
    assert payment.status == "pending_approval"


@pytest.mark.asyncio
async def test_receive_proof_rejects_unsupported_file_without_changing_status() -> None:
    payment = _awaiting()
    with pytest.raises(manual_payment_service.ManualPaymentError):
        await manual_payment_service.receive_proof(FakeSession(), FakeWhatsApp(mime="video/mp4"), payment, CONTACT, "m")
    assert payment.status == "awaiting_proof"


@pytest.mark.asyncio
async def test_webhook_proof_reply_is_human_and_has_no_emojis() -> None:
    from app.agent.text import strip_emojis

    payment, wa = _awaiting(), FakeWhatsApp()
    reply = await webhook._handle_payment_proof(FakeSession(), wa, payment, CONTACT, "m1")
    assert "espera de aprobación" in reply and payment.reference in reply
    assert strip_emojis(reply) == reply
    bad = await webhook._handle_payment_proof(FakeSession(), FakeWhatsApp(mime="video/mp4"), _awaiting(), CONTACT, "m1")
    assert "foto" in bad


@pytest.mark.asyncio
async def test_review_approve_and_reject_notify_student() -> None:
    wa = FakeWhatsApp()
    pending = _awaiting()
    pending.status = "pending_approval"
    await manual_payment_service.review(FakeSession(), wa, pending, CONTACT, approve=True)
    assert pending.status == "approved"
    assert wa.templates[0]["name"] == "pago_confirmado" and wa.templates[0]["to"] == "573012042870"
    assert wa.templates[0]["body_params"] == ["Ana", "cuota", "Bre-B", "PRX-B123"]
    assert wa.templates[0]["header_image_media_id"] == "uploaded-1"

    rejected = _awaiting()
    rejected.status = "pending_approval"
    await manual_payment_service.review(FakeSession(), wa, rejected, CONTACT, approve=False, note="El valor no coincide")
    assert rejected.status == "rejected"
    assert wa.templates[1]["name"] == "pago_no_confirmado"
    assert wa.templates[1]["body_params"] == ["Ana", "cuota", "PRX-B123", "El valor no coincide"]


@pytest.mark.asyncio
async def test_review_certificate_approval_sends_requirement_to_manager() -> None:
    wa, payment = FakeWhatsApp(), _awaiting()
    payment.concept, payment.status = "certificado", "pending_approval"
    await manual_payment_service.review(FakeSession(), wa, payment, CONTACT, approve=True)
    assert wa.staff and "Requerimiento pagado y confirmado" in wa.staff[0][1]
    assert not [t for t in wa.templates if t["name"] == "comprobante_pago"]  # ese texto dice "cuota"


@pytest.mark.asyncio
async def test_review_only_pending_payments() -> None:
    with pytest.raises(manual_payment_service.ManualPaymentError):
        await manual_payment_service.review(FakeSession(), FakeWhatsApp(), _awaiting(), CONTACT, approve=True)


@pytest.mark.asyncio
async def test_tool_start_manual_payment_returns_key_and_reference() -> None:
    ctx = SimpleNamespace(session=FakeSession(), contact=CONTACT, conversation=None)
    result = await tools.execute_tool(
        "start_manual_payment",
        {"method": "breb", "concept": "cuota", "national_id": "1020304050", "contract_number": "12345", "account_number": "001"},
        ctx,
    )
    assert result["status"] == "awaiting_proof"
    assert "0089087583" in result["destination"] and "PRAXIS SCHOOL" in result["destination"]
    assert result["reference"].startswith("PRX-B")
    bad = await tools.execute_tool(
        "start_manual_payment", {"method": "breb", "concept": "cuota", "national_id": "abc", "contract_number": "1"}, ctx
    )
    assert bad["error"] == "invalid_data"


def test_prompt_recommends_breb_offers_all_methods_and_forbids_claiming_confirmation() -> None:
    for expected in ("Bre-B", "Nequi", "Daviplata", "start_manual_payment", "por confirmar"):
        assert expected in SYSTEM_PROMPT, expected
    assert "Nunca digas que un pago por transferencia está confirmado" in SYSTEM_PROMPT


def test_admin_endpoints_require_api_key_and_handle_review(monkeypatch) -> None:
    payment = _awaiting()
    payment.id = uuid.uuid4()
    payment.contact_id = CONTACT.id
    payment.status = "pending_approval"
    payment.created_at = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    sent = []

    class Session:
        committed = 0

        async def get(self, model, key):
            return payment if model is ManualPayment else CONTACT

        async def flush(self):
            pass

        async def commit(self):
            Session.committed += 1

    async def fake_session():
        yield Session()

    async def fake_send(self, to, body):
        sent.append(body)

    posted = []

    async def fake_post(self, path, json=None, files=None, data=None):
        posted.append(json)
        return {"id": "m1"}

    monkeypatch.setattr("app.whatsapp.client.WhatsAppClient.send_text", fake_send)
    monkeypatch.setattr("app.whatsapp.client.WhatsAppClient._post", fake_post)
    app.dependency_overrides[get_session] = fake_session
    try:
        client = TestClient(app)
        url = f"/admin/manual-payments/{payment.id}/approve"
        assert client.post(url).status_code in (401, 422)
        ok = client.post(url, headers={"X-API-Key": "test"})
        assert ok.status_code == 200 and ok.json()["status"] == "approved"
        templates = [p["template"]["name"] for p in posted if p and p.get("type") == "template"]
        assert templates == ["pago_confirmado"]
        assert Session.committed == 1
        again = client.post(url, headers={"X-API-Key": "test"})
        assert again.status_code == 409
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method, expected",
    [("nequi", "Nequi: @3002666815"), ("daviplata", "Daviplata: 3002666815"), ("breb", "0089087583")],
)
async def test_each_method_gives_the_right_destination(method, expected) -> None:
    payment = await breb_start(method)
    assert payment.method == method
    assert expected in manual_payment_service.instructions(payment)


async def breb_start(method):
    return await manual_payment_service.start_payment(
        FakeSession(), CONTACT, None, "cuota", "1020304050", "12345", "001", method=method
    )


@pytest.mark.asyncio
async def test_unknown_method_is_rejected() -> None:
    with pytest.raises(manual_payment_service.ManualPaymentError):
        await breb_start("paypal")


@pytest.mark.asyncio
async def test_reusing_pending_payment_switches_method() -> None:
    existing = ManualPayment(reference="PRX-BOLD", concept="cuota", method="breb", status="awaiting_proof")
    payment = await manual_payment_service.start_payment(
        FakeSession(existing=existing), CONTACT, None, "cuota", "1020304050", "12345", "001", method="nequi"
    )
    assert payment is existing and payment.method == "nequi"


@pytest.mark.asyncio
async def test_student_gets_receipt_por_confirmar_after_sending_proof(tmp_path) -> None:
    payment, wa = _awaiting(), FakeWhatsApp()
    await manual_payment_service.receive_proof(FakeSession(), wa, payment, CONTACT, "media-9")
    assert (tmp_path / "receipts" / "PRX-B123.png").read_bytes().startswith(b"\x89PNG")
    assert "573012042870" in wa.images
    assert "por confirmar" in wa.captions[0]


@pytest.mark.asyncio
async def test_approval_sends_confirmed_receipt_to_student_and_service_number(monkeypatch) -> None:
    monkeypatch.setattr(manual_payment_service.settings, "payment_receipt_numbers", "573102394548")
    wa, payment = FakeWhatsApp(), _awaiting()
    payment.status = "pending_approval"
    await manual_payment_service.review(FakeSession(), wa, payment, CONTACT, approve=True)
    service = [t for t in wa.templates if t["to"] == "573102394548"]
    assert service and service[0]["name"] == "comprobante_pago"
    assert [t["name"] for t in wa.templates if t["to"] == "573012042870"] == ["pago_confirmado"]


def test_manual_receipt_images_render_both_states() -> None:
    from app.services.receipt_image import render_manual_receipt

    payment = _awaiting()
    payment.reviewed_at = payment.proof_received_at = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    for confirmed in (False, True):
        assert render_manual_receipt(payment, CONTACT, confirmed=confirmed).startswith(b"\x89PNG")


# --- aprobación por WhatsApp del gestor ---

MANAGER = "573102394548"


@pytest.mark.parametrize(
    "text, expected",
    [
        ("aprobar PRX-B0123456789", ("approve", "PRX-B0123456789", None)),
        ("  Confirmar prx-babcdef0123  ", ("approve", "PRX-BABCDEF0123", None)),
        ("RECHAZAR PRX-B0123456789 el valor no coincide", ("reject", "PRX-B0123456789", "el valor no coincide")),
        ("rechazar PRX-B0123456789: sin fondos", ("reject", "PRX-B0123456789", "sin fondos")),
        ("pendientes", ("list", None, None)),
        ("Pendientes.", ("list", None, None)),
    ],
)
def test_parse_manager_command(text, expected) -> None:
    assert manual_payment_service.parse_manager_command(text) == expected


@pytest.mark.parametrize(
    "text", [None, "", "hola", "aprobar", "aprobar PRX-B12", "aprobar PRX-1234", "quiero aprobar PRX-B0123456789", "aprobar pagos pendientes"]
)
def test_parse_manager_command_ignores_normal_messages(text) -> None:
    assert manual_payment_service.parse_manager_command(text) is None


class CommandSession(FakeSession):
    def __init__(self, payment=None, pending=None):
        super().__init__(existing=payment)
        self.pending = pending or []

    async def execute(self, _stmt):
        return _Result(self.existing, self.pending)

    async def get(self, model, key):
        return CONTACT


@pytest.mark.asyncio
async def test_only_manager_numbers_can_run_commands() -> None:
    payment = _awaiting()
    payment.status = "pending_approval"
    for sender in ("573012042870", "573999999999"):
        reply = await manual_payment_service.handle_manager_command(
            CommandSession(payment), FakeWhatsApp(), sender, "aprobar PRX-B0123456789"
        )
        assert reply is None
    assert payment.status == "pending_approval"


@pytest.mark.asyncio
async def test_manager_approves_by_whatsapp() -> None:
    payment, wa = _awaiting(), FakeWhatsApp()
    payment.status = "pending_approval"
    reply = await manual_payment_service.handle_manager_command(
        CommandSession(payment), wa, MANAGER, "aprobar PRX-B0123456789"
    )
    assert payment.status == "approved"
    assert "quedó confirmado" in reply
    assert wa.templates[0]["name"] == "pago_confirmado"


@pytest.mark.asyncio
async def test_manager_rejects_with_reason_by_whatsapp() -> None:
    payment, wa = _awaiting(), FakeWhatsApp()
    payment.status = "pending_approval"
    reply = await manual_payment_service.handle_manager_command(
        CommandSession(payment), wa, MANAGER, "rechazar PRX-B0123456789 captura ilegible"
    )
    assert payment.status == "rejected" and "rechazado" in reply
    assert wa.templates[0]["body_params"][-1] == "captura ilegible"


@pytest.mark.asyncio
async def test_manager_gets_clear_answers_for_unknown_or_closed_payments() -> None:
    unknown = await manual_payment_service.handle_manager_command(
        CommandSession(None), FakeWhatsApp(), MANAGER, "aprobar PRX-B0123456789"
    )
    assert "No encontré" in unknown
    done = _awaiting()
    done.status = "approved"
    closed = await manual_payment_service.handle_manager_command(
        CommandSession(done), FakeWhatsApp(), MANAGER, "aprobar PRX-B0123456789"
    )
    assert "ya no está pendiente" in closed


@pytest.mark.asyncio
async def test_manager_lists_pending_payments() -> None:
    empty = await manual_payment_service.handle_manager_command(CommandSession(), FakeWhatsApp(), MANAGER, "pendientes")
    assert "No hay pagos pendientes" in empty
    pending = _awaiting()
    pending.status = "pending_approval"
    listed = await manual_payment_service.handle_manager_command(
        CommandSession(pending=[pending]), FakeWhatsApp(), MANAGER, "pendientes"
    )
    assert "PRX-B123" in listed and "Ana Pérez" in listed


@pytest.mark.asyncio
async def test_forwarded_notice_tells_the_manager_how_to_answer() -> None:
    payment, wa = _awaiting(), FakeWhatsApp()
    await manual_payment_service.receive_proof(FakeSession(), wa, payment, CONTACT, "m")
    assert "aprobar PRX-B123" in wa.staff[0][1] and "rechazar PRX-B123" in wa.staff[0][1]


@pytest.mark.asyncio
async def test_generated_references_are_understood_by_the_command_parser() -> None:
    for _ in range(20):
        payment = await breb_start("breb")
        assert manual_payment_service.parse_manager_command(f"aprobar {payment.reference}") == (
            "approve",
            payment.reference,
            None,
        )


@pytest.mark.asyncio
async def test_result_falls_back_to_text_and_receipt_when_template_is_not_available() -> None:
    wa, payment = FakeWhatsApp(template_fails=True), _awaiting()
    payment.status = "pending_approval"
    await manual_payment_service.review(FakeSession(), wa, payment, CONTACT, approve=True)
    assert "confirmado" in wa.texts[0][1]
    assert "573012042870" in wa.images

    rejected, wa2 = _awaiting(), FakeWhatsApp(template_fails=True)
    rejected.status = "pending_approval"
    await manual_payment_service.review(FakeSession(), wa2, rejected, CONTACT, approve=False, note="captura ilegible")
    assert "captura ilegible" in wa2.texts[0][1]
