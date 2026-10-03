from fastapi import FastAPI

from app.config import get_settings
from app.logging_config import configure_logging
from app.routers import (
    admin_documents,
    admin_leads,
    admin_manual_payments,
    admin_payments,
    health,
    payments,
    webhook,
)

settings = get_settings()
configure_logging(settings.log_level)

# Sin /docs ni /openapi.json públicos: la API es interna (Meta, Wompi y panel admin).
app = FastAPI(title="Praxis WhatsApp Agent", version="0.1.0", docs_url=None, redoc_url=None, openapi_url=None)

app.include_router(health.router)
app.include_router(webhook.router)
app.include_router(admin_documents.router)
app.include_router(admin_leads.router)
app.include_router(admin_payments.router)
app.include_router(admin_manual_payments.router)
app.include_router(payments.router)
