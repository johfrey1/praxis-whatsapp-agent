import os

os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("WHATSAPP_PHONE_NUMBER_ID", "test")
os.environ.setdefault("WHATSAPP_ACCESS_TOKEN", "test")
os.environ.setdefault("WHATSAPP_APP_SECRET", "test")
os.environ.setdefault("WHATSAPP_VERIFY_TOKEN", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("STRAPI_BASE_URL", "http://localhost:1337")
os.environ.setdefault("ADMIN_API_KEY", "test")

from datetime import datetime, timezone

import httpx
import pytest

from app.wompi.client import WompiClient


@pytest.mark.asyncio
async def test_open_amount_link_sends_neither_currency_nor_amount(monkeypatch) -> None:
    """Wompi responde 422 si se envía currency sin amount_in_cents."""
    captured = {}

    async def fake_post(self, url, headers=None, json=None, **kwargs):
        captured["json"] = json
        return httpx.Response(200, json={"data": {"id": "test_AbC123"}}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    link = await WompiClient().create_payment_link(
        name="Cuota", description="d", sku="PRX-1", expires_at=datetime(2026, 10, 4, tzinfo=timezone.utc)
    )

    assert link["id"] == "test_AbC123"
    assert "currency" not in captured["json"]
    assert "amount_in_cents" not in captured["json"]
    assert captured["json"]["single_use"] is True
    assert captured["json"]["expires_at"] == "2026-10-04T00:00:00.000Z"
