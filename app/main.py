from fastapi import FastAPI

from app.config import get_settings
from app.logging_config import configure_logging
from app.routers import admin_documents, admin_leads, health, webhook

settings = get_settings()
configure_logging(settings.log_level)

app = FastAPI(title="Praxis WhatsApp Agent", version="0.1.0")

app.include_router(health.router)
app.include_router(webhook.router)
app.include_router(admin_documents.router)
app.include_router(admin_leads.router)
