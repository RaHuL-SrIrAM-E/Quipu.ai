from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Quipu"
    environment: str = "development"

    database_url: str = "sqlite:///./quipu.db"

    gcp_project_id: str | None = None
    gcp_location: str = "us-central1"
    gemini_model: str = "gemini-2.5-pro"
    google_application_credentials: str | None = None

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
