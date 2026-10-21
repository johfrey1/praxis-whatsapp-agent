import uuid
from types import SimpleNamespace

import pytest

from app.agent import tools
from app.services import conversation_service, lead_service


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("3202790995", "573202790995"),
        ("320 279 0995", "573202790995"),
        ("+57 320-279-0995", "573202790995"),
        ("573202790995", "573202790995"),
        ("6017451234", "576017451234"),
        ("+1 305 555 1234", "13055551234"),
        ("12345", None),
        ("2790995", None),
        ("no tengo", None),
        ("", None),
    ],
)
def test_normalize_phone(raw, expected) -> None:
    assert lead_service.normalize_phone(raw) == expected


@pytest.fixture
def saved(monkeypatch):
    created: list[dict] = []

    async def fake_save_lead(session, **kwargs):
        created.append(kwargs)
        return SimpleNamespace(id=uuid.uuid4(), **kwargs), True, True

    async def fake_known_phone(session, contact_id):
        return None

    async def fake_notify(*args, **kwargs):
        return None

    monkeypatch.setattr(lead_service, "save_lead", fake_save_lead)
    monkeypatch.setattr(lead_service, "known_phone", fake_known_phone)
    monkeypatch.setattr(conversation_service, "notify_staff", fake_notify)
    return created


def _ctx(wa_id: str):
    contact = SimpleNamespace(id=uuid.uuid4(), wa_id=wa_id, username="maria", profile_name="Maria")
    return SimpleNamespace(session=None, contact=contact, whatsapp_client=None)


LEAD = {"full_name": "Maria Cardona", "email": "maria@example.com"}


@pytest.mark.asyncio
async def test_lead_without_whatsapp_phone_requires_phone(saved) -> None:
    result = await tools.execute_tool("save_lead", LEAD, _ctx("CO.2533507690468069"))
    assert result == {"error": "phone_required"}
    assert saved == []


@pytest.mark.asyncio
async def test_invalid_phone_is_rejected(saved) -> None:
    result = await tools.execute_tool("save_lead", {**LEAD, "phone": "12345"}, _ctx("CO.2533507690468069"))
    assert result == {"error": "invalid_phone"}
    assert saved == []


@pytest.mark.asyncio
async def test_given_phone_is_saved_normalized(saved) -> None:
    result = await tools.execute_tool("save_lead", {**LEAD, "phone": "320 279 0995"}, _ctx("CO.2533507690468069"))
    assert result["status"] == "saved"
    assert saved[0]["phone"] == "573202790995"


@pytest.mark.asyncio
async def test_whatsapp_phone_is_used_when_none_given(saved) -> None:
    result = await tools.execute_tool("save_lead", LEAD, _ctx("573144434782"))
    assert result["status"] == "saved"
    assert saved[0]["phone"] == "573144434782"


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _Session:
    """Sesión mínima: devuelve `existing` como prospecto abierto y registra lo que se agrega."""

    def __init__(self, existing=None):
        self.existing = existing
        self.added = []

    async def execute(self, _stmt):
        return _Result(self.existing)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        return None


@pytest.mark.asyncio
async def test_first_save_creates_and_notifies() -> None:
    session = _Session()
    lead, created, notify = await lead_service.save_lead(session, uuid.uuid4(), full_name="Camila", phone="573135718556")
    assert (created, notify) == (True, True)
    assert session.added == [lead]


@pytest.mark.asyncio
async def test_repeated_save_updates_the_same_lead_without_new_alert() -> None:
    existing = SimpleNamespace(full_name="Camila", phone="573135718556", email="c@x.com", comments=None)
    session = _Session(existing)
    lead, created, notify = await lead_service.save_lead(
        session, uuid.uuid4(), full_name="Camila Loaiza Gaona", phone="573135718556", email="c@x.com", comments="sábados"
    )
    assert lead is existing and session.added == []
    assert (created, notify) == (False, False)
    assert existing.full_name == "Camila Loaiza Gaona" and existing.comments == "sábados"


@pytest.mark.asyncio
async def test_new_phone_on_existing_lead_alerts_again() -> None:
    existing = SimpleNamespace(full_name="Santiago", phone=None, email="s@x.com")
    lead, created, notify = await lead_service.save_lead(_Session(existing), uuid.uuid4(), phone="573202790995")
    assert (created, notify) == (False, True)
    assert lead.phone == "573202790995"
