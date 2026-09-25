from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Anthropic
    anthropic_api_key: str
    claude_model: str = "claude-sonnet-5"
    claude_max_tokens: int = 1024

    # WhatsApp Cloud API
    whatsapp_phone_number_id: str
    whatsapp_business_account_id: str = ""
    whatsapp_access_token: str
    whatsapp_app_secret: str
    whatsapp_verify_token: str
    whatsapp_graph_api_version: str = "v21.0"
    staff_notification_numbers: str = ""

    # Database
    database_url: str

    # Strapi (read-only integration)
    strapi_base_url: str
    strapi_api_token: str = ""

    # Wompi (pago de cuotas)
    wompi_environment: str = "sandbox"  # sandbox | production
    wompi_public_key: str = ""
    wompi_private_key: str = ""
    wompi_events_secret: str = ""
    wompi_payment_link_ttl_hours: int = 24
    wompi_redirect_url: str = ""

    # Admin API
    admin_api_key: str

    # App
    environment: str = "development"
    log_level: str = "INFO"
    document_storage_path: str = "/app/storage/documents"

    @property
    def staff_numbers(self) -> list[str]:
        return [n.strip() for n in self.staff_notification_numbers.split(",") if n.strip()]

    @property
    def graph_api_base_url(self) -> str:
        return f"https://graph.facebook.com/{self.whatsapp_graph_api_version}"

    @property
    def wompi_api_base_url(self) -> str:
        host = "production" if self.wompi_environment == "production" else "sandbox"
        return f"https://{host}.wompi.co/v1"

    @property
    def payments_enabled(self) -> bool:
        return bool(self.wompi_private_key and self.wompi_events_secret)


@lru_cache
def get_settings() -> Settings:
    return Settings()
