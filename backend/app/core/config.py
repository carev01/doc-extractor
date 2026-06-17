"""Application configuration."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """App settings loaded from environment variables."""

    # Database — required; set DOCEXTRACTOR_DATABASE_URL and DOCEXTRACTOR_DATABASE_URL_SYNC
    database_url: str
    database_url_sync: str

    # Firecrawl — required; set DOCEXTRACTOR_FIRECRAWL_API_URL
    firecrawl_api_url: str
    firecrawl_api_key: str = ""
    # Base URL Firecrawl can call back for webhook events (e.g. http://172.16.255.190:8000).
    # Leave empty to disable webhooks and use cursor polling for progress instead.
    webhook_base_url: str = ""

    # CORS — comma-separated or JSON list via DOCEXTRACTOR_CORS_ORIGINS
    cors_origins: list[str] = ["http://localhost:5173", "http://localhost:3000"]

    # Export
    export_dir: str = "exports"
    max_articles_per_file: int = 50
    max_file_size_bytes: int = 10 * 1024 * 1024  # 10 MB
    max_tokens_per_file: int = 100_000

    # Image storage — canonical (source-of-truth) images live in media_dir,
    # kept separate from generated exports/. Served over HTTP at media_url_prefix
    # so the frontend can render them and exports can rewrite to relative paths.
    media_dir: str = "media"
    media_url_prefix: str = "/media"

    model_config = {
        "env_prefix": "DOCEXTRACTOR_",
        "case_sensitive": False,
        "env_file": ".env",
        "env_file_encoding": "utf-8",
    }


settings = Settings()
