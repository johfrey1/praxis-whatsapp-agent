import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import conversation_service


def test_api_docs_are_not_public() -> None:
    client = TestClient(app)
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404, path


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeSession:
    def __init__(self, found):
        self._found = found
        self.queries = 0

    async def execute(self, _stmt):
        self.queries += 1
        return _FakeResult(self._found)


@pytest.mark.asyncio
async def test_message_already_processed_detects_duplicates() -> None:
    assert await conversation_service.message_already_processed(_FakeSession(found=7), "wamid.1") is True
    assert await conversation_service.message_already_processed(_FakeSession(found=None), "wamid.1") is False


@pytest.mark.asyncio
async def test_message_without_id_skips_the_query() -> None:
    session = _FakeSession(found=7)
    assert await conversation_service.message_already_processed(session, None) is False
    assert session.queries == 0
