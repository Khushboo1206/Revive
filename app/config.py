from functools import lru_cache
from pathlib import Path
from urllib.parse import quote_plus

from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    app_name: str = "Revive"
    environment: str = "development"
    database_url: str = "postgresql+psycopg://revive:revive@localhost:5432/revive"
    postgres_user: str = "postgres"
    postgres_password: str = ""
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_database: str = "revive"
    razorpay_webhook_secret: str = "replace-me"
    recovery_engine_enabled: bool = False
    recovery_engine_interval_seconds: int = 60
    ai_confidence_threshold: float = 0.60
    promise_to_pay_weight: float = 0.40
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"
    gemini_timeout_seconds: int = 30

    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()


def get_database_url() -> str:
    settings = get_settings()
    if settings.postgres_password:
        password = quote_plus(settings.postgres_password)
        return f"postgresql+psycopg://{quote_plus(settings.postgres_user)}:{password}@{settings.postgres_host}:{settings.postgres_port}/{settings.postgres_database}"
    return settings.database_url
