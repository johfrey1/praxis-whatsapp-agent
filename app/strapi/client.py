"""Cliente de solo lectura hacia la API REST de Strapi (plataforma existente).

Deliberadamente no expone escritura: el agente de WhatsApp nunca debe crear o
modificar contratos, facturas ni datos de personas directamente en Strapi.
"""

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import get_settings
from app.logging_config import get_logger

settings = get_settings()
logger = get_logger(__name__)

_retry = retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=1, max=4))


class StrapiClient:
    def __init__(self) -> None:
        self._base_url = settings.strapi_base_url.rstrip("/")
        self._headers = {}
        if settings.strapi_api_token:
            self._headers["Authorization"] = f"Bearer {settings.strapi_api_token}"

    @_retry
    async def _get(self, path: str, params: dict | None = None) -> dict:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(f"{self._base_url}/api/{path}", headers=self._headers, params=params)
        if response.is_error:
            logger.error("strapi_api_error", path=path, status=response.status_code, body=response.text)
        response.raise_for_status()
        return response.json()

    async def list_class_schedules(self, filters: dict | None = None) -> list[dict]:
        params = {"populate": "*"}
        if filters:
            for key, value in filters.items():
                params[f"filters[{key}][$containsi]"] = value
        data = await self._get("class-schedules", params)
        return data.get("data", [])

    async def list_teachers(self) -> list[dict]:
        data = await self._get("teachers", {"populate": "*"})
        return data.get("data", [])

    async def list_categories(self) -> list[dict]:
        data = await self._get("categories", {"populate": "*"})
        return data.get("data", [])

    async def list_articles(self, category: str | None = None) -> list[dict]:
        params: dict = {"populate": "*"}
        if category:
            params["filters[category][name][$containsi]"] = category
        data = await self._get("articles", params)
        return data.get("data", [])
