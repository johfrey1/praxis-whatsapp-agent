"""Modelos permisivos para el payload de webhook de la WhatsApp Cloud API.

Meta agrega campos con frecuencia; se usa `extra='allow'` para no romper el
parseo cuando lleguen campos nuevos que no necesitamos.
"""

from pydantic import BaseModel, ConfigDict


class LaxModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class WebhookEnvelope(LaxModel):
    object: str | None = None
    entry: list["WebhookEntry"] = []


class WebhookEntry(LaxModel):
    id: str | None = None
    changes: list["WebhookChange"] = []


class WebhookChange(LaxModel):
    field: str | None = None
    value: "WebhookValue" = None  # type: ignore[assignment]


class WebhookValue(LaxModel):
    messaging_product: str | None = None
    metadata: dict | None = None
    contacts: list[dict] = []
    messages: list[dict] = []
    statuses: list[dict] = []


WebhookEnvelope.model_rebuild()
WebhookEntry.model_rebuild()
WebhookChange.model_rebuild()
