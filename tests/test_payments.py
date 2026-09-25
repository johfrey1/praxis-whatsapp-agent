import hashlib
import os

os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("WHATSAPP_PHONE_NUMBER_ID", "test")
os.environ.setdefault("WHATSAPP_ACCESS_TOKEN", "test")
os.environ.setdefault("WHATSAPP_APP_SECRET", "test")
os.environ.setdefault("WHATSAPP_VERIFY_TOKEN", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("STRAPI_BASE_URL", "http://localhost:1337")
os.environ.setdefault("ADMIN_API_KEY", "test")

import pytest
from fastapi.testclient import TestClient

from app.db.base import get_session
from app.main import app
from app.routers import payments as payments_router
from app.services import payment_service

EVENTS_SECRET = "test_events_secret"


def _event(status: str = "APPROVED") -> dict:
    event = {
        "event": "transaction.updated",
        "data": {
            "transaction": {
                "id": "1234-1610641025-49201",
                "amount_in_cents": 4490000,
                "status": status,
                "payment_link_id": "test_abc123",
            }
        },
        "environment": "test",
        "signature": {"properties": ["transaction.id", "transaction.status", "transaction.amount_in_cents"]},
        "timestamp": 1530291411,
    }
    raw = f"1234-1610641025-49201{status}44900001530291411{EVENTS_SECRET}"
    event["signature"]["checksum"] = hashlib.sha256(raw.encode()).hexdigest().upper()
    return event


def test_checksum_valid_and_tampered() -> None:
    event = _event()
    assert payment_service.verify_event_checksum(event, EVENTS_SECRET)

    event["data"]["transaction"]["amount_in_cents"] = 100
    assert not payment_service.verify_event_checksum(event, EVENTS_SECRET)


def test_checksum_rejected_without_secret() -> None:
    assert not payment_service.verify_event_checksum(_event(), "")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (("1.023.456.789", "ctr-001", " 55 01 "), ("1023456789", "CTR-001", "5501")),
        (("80 123 456", "A1", "B2"), ("80123456", "A1", "B2")),
    ],
)
def test_normalize_payment_data(raw, expected) -> None:
    assert payment_service.normalize_payment_data(*raw) == expected


@pytest.mark.parametrize(
    "raw",
    [("abc", "C1", "A1"), ("123", "C1", "A1"), ("1023456789", "C 1/2", "A1"), ("1023456789", "C1", "")],
)
def test_normalize_payment_data_rejects_invalid(raw) -> None:
    with pytest.raises(payment_service.PaymentValidationError):
        payment_service.normalize_payment_data(*raw)


def test_format_cop() -> None:
    assert payment_service.format_cop(15000000) == "$150.000 COP"


@pytest.fixture
def client(monkeypatch):
    class FakeSession:
        commits = 0

        async def commit(self):
            FakeSession.commits += 1

        async def rollback(self):
            pass

    async def fake_session():
        yield FakeSession()

    monkeypatch.setattr(payments_router.settings, "wompi_events_secret", EVENTS_SECRET)
    app.dependency_overrides[get_session] = fake_session
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_events_endpoint_rejects_bad_checksum(client) -> None:
    event = _event()
    event["signature"]["checksum"] = "0" * 64
    assert client.post("/payments/wompi/events", json=event).status_code == 401


def test_events_endpoint_processes_and_notifies(client, monkeypatch) -> None:
    calls = []

    async def fake_process(session, wompi_client, event):
        calls.append(("process", event["data"]["transaction"]["status"]))
        return "payment"

    async def fake_notify(session, whatsapp_client, payment):
        calls.append(("notify", payment))

    monkeypatch.setattr(payment_service, "process_wompi_event", fake_process)
    monkeypatch.setattr(payment_service, "notify_payment_result", fake_notify)

    response = client.post("/payments/wompi/events", json=_event())
    assert response.status_code == 200
    assert calls == [("process", "APPROVED"), ("notify", "payment")]


def test_events_endpoint_returns_500_so_wompi_retries(client, monkeypatch) -> None:
    async def failing_process(session, wompi_client, event):
        raise RuntimeError("db down")

    monkeypatch.setattr(payment_service, "process_wompi_event", failing_process)
    assert client.post("/payments/wompi/events", json=_event()).status_code == 500
