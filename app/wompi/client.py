"""Cliente para la API de Wompi (Colombia): links de pago y consulta de transacciones."""

from datetime import datetime

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import get_settings
from app.logging_config import get_logger

settings = get_settings()
logger = get_logger(__name__)

_retry = retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=4))

CHECKOUT_LINK_BASE_URL = "https://checkout.wompi.co/l"


def checkout_url(payment_link_id: str) -> str:
    return f"{CHECKOUT_LINK_BASE_URL}/{payment_link_id}"


class WompiClient:
    def __init__(self) -> None:
        self._base_url = settings.wompi_api_base_url
        self._private_headers = {"Authorization": f"Bearer {settings.wompi_private_key}"}

    # Sin retry: reintentar un POST que hizo timeout podría crear links duplicados.
    async def create_payment_link(self, *, name: str, description: str, sku: str, expires_at: datetime) -> dict:
        """Crea un link de un solo uso SIN amount_in_cents: el pagador escribe el valor a pagar."""
        body = {
            "name": name,
            "description": description,
            "single_use": True,
            "collect_shipping": False,
            "currency": "COP",
            "sku": sku,
            "expires_at": expires_at.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        }
        if settings.wompi_redirect_url:
            body["redirect_url"] = settings.wompi_redirect_url

        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(f"{self._base_url}/payment_links", headers=self._private_headers, json=body)
        if response.is_error:
            logger.error("wompi_api_error", path="payment_links", status=response.status_code, body=response.text)
        response.raise_for_status()
        return response.json()["data"]

    @_retry
    async def get_transaction(self, transaction_id: str) -> dict:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(f"{self._base_url}/transactions/{transaction_id}")
        if response.is_error:
            logger.error("wompi_api_error", path="transactions", status=response.status_code, body=response.text)
        response.raise_for_status()
        return response.json()["data"]
