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
from app.services import breb_service

CONTACT = SimpleNamespace(id=uuid.uuid4(), wa_id="573012042870", profile_name="Juan")


class _Result:
    def __init__(self, value):
        self._value = value

    def scalars(self):
        return self

    def first(self):
        return self._value


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
        self.texts, self.staff, self.templates, self.images = [], [], [], []

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

    async def send_text(self, to, body):
        self.texts.append((to, body))


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    s = breb_service.settings
    monkeypatch.setattr(s, "document_storage_path", str(tmp_path))
    monkeypatch.setattr(s, "student_manager_numbers", "573102394548")


@pytest.mark.asyncio
async def test_start_cuota_payment_normalizes_and_returns_instructions() -> None:
    session = FakeSession()
    payment = await breb_service.start_payment(
        session, CONTACT, None, "cuota", "1.020.304.050", "prx-12", "001", amount_cop=150000
    )
    assert session.added == [payment]
    assert (payment.national_id, payment.contract_number, payment.account_number) == ("1020304050", "PRX-12", "001")
    assert payment.amount_in_cents == 15000000
    assert payment.status == "awaiting_proof"
    text = breb_service.instructions(payment)
    assert "0089087583" in text and "PRAXIS SCHOOL" in text and payment.reference in text


@pytest.mark.asyncio
async def test_start_certificate_uses_fixed_fee_and_requires_full_name() -> None:
    payment = await breb_service.start_payment(
        FakeSession(), CONTACT, None, "certificado", "1020304050", "12345", student_name="Ana María Pérez"
    )
    assert payment.amount_in_cents == 1800000
    with pytest.raises(breb_service.BrebValidationError):
        await breb_service.start_payment(FakeSession(), CONTACT, None, "certificado", "1020304050", "12345", student_name="Ana")


@pytest.mark.asyncio
async def test_start_reuses_payment_awaiting_proof() -> None:
    existing = ManualPayment(reference="PRX-BOLD", concept="cuota", status="awaiting_proof")
    session = FakeSession(existing=existing)
    payment = await breb_service.start_payment(session, CONTACT, None, "cuota", "1020304050", "12345", "001")
    assert payment is existing and session.added == []


@pytest.mark.asyncio
async def test_start_rejects_unknown_concept() -> None:
    with pytest.raises(breb_service.BrebValidationError):
        await breb_service.start_payment(FakeSession(), CONTACT, None, "sabana_notas", "1020304050", "12345")


def _awaiting() -> ManualPayment:
    return ManualPayment(
        reference="PRX-B123",
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
    await breb_service.receive_proof(FakeSession(), wa, payment, CONTACT, "media-9")

    assert payment.status == "pending_approval"
    assert payment.proof_media_id == "media-9"
    assert (tmp_path / "proofs" / "PRX-B123.jpg").read_bytes() == b"fake-bytes"
    assert wa.staff[0][0] == "573102394548" and "PRX-B123" in wa.staff[0][1] and "Bre-B" in wa.staff[0][1]
    assert wa.templates[0]["name"] == "comprobante_breb"
    assert wa.templates[0]["header_image_media_id"] == "uploaded-1"
    assert wa.templates[0]["body_params"] == ["Ana Pérez", "1020304050", "12345", "Cuota", "PRX-B123"]


@pytest.mark.asyncio
async def test_receive_proof_falls_back_to_free_image_when_template_fails() -> None:
    payment, wa = _awaiting(), FakeWhatsApp(template_fails=True)
    await breb_service.receive_proof(FakeSession(), wa, payment, CONTACT, "media-9")
    assert wa.images == ["573102394548"]
    assert payment.status == "pending_approval"


@pytest.mark.asyncio
async def test_receive_proof_rejects_unsupported_file_without_changing_status() -> None:
    payment = _awaiting()
    with pytest.raises(breb_service.BrebValidationError):
        await breb_service.receive_proof(FakeSession(), FakeWhatsApp(mime="video/mp4"), payment, CONTACT, "m")
    assert payment.status == "awaiting_proof"


@pytest.mark.asyncio
async def test_webhook_proof_reply_is_human_and_has_no_emojis() -> None:
    from app.agent.text import strip_emojis

    payment, wa = _awaiting(), FakeWhatsApp()
    reply = await webhook._handle_breb_proof(FakeSession(), wa, payment, CONTACT, "m1")
    assert "espera de aprobación" in reply and payment.reference in reply
    assert strip_emojis(reply) == reply
    bad = await webhook._handle_breb_proof(FakeSession(), FakeWhatsApp(mime="video/mp4"), _awaiting(), CONTACT, "m1")
    assert "foto" in bad


@pytest.mark.asyncio
async def test_review_approve_and_reject_notify_student() -> None:
    wa = FakeWhatsApp()
    pending = _awaiting()
    pending.status = "pending_approval"
    await breb_service.review(FakeSession(), wa, pending, CONTACT, approve=True)
    assert pending.status == "approved" and "aprobado" in wa.texts[0][1]

    rejected = _awaiting()
    rejected.status = "pending_approval"
    await breb_service.review(FakeSession(), wa, rejected, CONTACT, approve=False, note="El valor no coincide")
    assert rejected.status == "rejected" and "El valor no coincide" in wa.texts[1][1]


@pytest.mark.asyncio
async def test_review_certificate_approval_sends_requirement_to_manager() -> None:
    wa, payment = FakeWhatsApp(), _awaiting()
    payment.concept, payment.status = "certificado", "pending_approval"
    await breb_service.review(FakeSession(), wa, payment, CONTACT, approve=True)
    assert wa.staff and "Requerimiento pagado y aprobado" in wa.staff[0][1]


@pytest.mark.asyncio
async def test_review_only_pending_payments() -> None:
    with pytest.raises(breb_service.BrebValidationError):
        await breb_service.review(FakeSession(), FakeWhatsApp(), _awaiting(), CONTACT, approve=True)


@pytest.mark.asyncio
async def test_tool_start_breb_payment_returns_key_and_reference() -> None:
    ctx = SimpleNamespace(session=FakeSession(), contact=CONTACT, conversation=None)
    result = await tools.execute_tool(
        "start_breb_payment",
        {"concept": "cuota", "national_id": "1020304050", "contract_number": "12345", "account_number": "001"},
        ctx,
    )
    assert result["status"] == "awaiting_proof"
    assert result["breb_key"] == "0089087583" and result["account_name"] == "PRAXIS SCHOOL"
    assert result["reference"].startswith("PRX-B")
    bad = await tools.execute_tool(
        "start_breb_payment", {"concept": "cuota", "national_id": "abc", "contract_number": "1"}, ctx
    )
    assert bad["error"] == "invalid_data"


def test_prompt_recommends_breb_and_forbids_claiming_approval() -> None:
    assert "Bre-B" in SYSTEM_PROMPT and "start_breb_payment" in SYSTEM_PROMPT
    assert "Nunca digas que el pago está aprobado por Bre-B" in SYSTEM_PROMPT


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

    monkeypatch.setattr("app.whatsapp.client.WhatsAppClient.send_text", fake_send)
    app.dependency_overrides[get_session] = fake_session
    try:
        client = TestClient(app)
        url = f"/admin/manual-payments/{payment.id}/approve"
        assert client.post(url).status_code in (401, 422)
        ok = client.post(url, headers={"X-API-Key": "test"})
        assert ok.status_code == 200 and ok.json()["status"] == "approved"
        assert sent and "aprobado" in sent[0]
        assert Session.committed == 1
        again = client.post(url, headers={"X-API-Key": "test"})
        assert again.status_code == 409
    finally:
        app.dependency_overrides.clear()
