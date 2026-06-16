"""Application configuration."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """App settings loaded from environment variables."""

    # Database
    database_url: str = "postgresql+asyncpg://docextractor:docextractor_dev@localhost:5432/docextractor"
    database_url_sync: str = "postgresql+psycopg2://docextractor:docextractor_dev@localhost:5432/docextractor"

    # Firecrawl
    firecrawl_api_url: str = "http://firecrawl.k3s.home.lan"
    firecrawl_api_key: str = ""

    # Export
    export_dir: str = "exports"
    max_articles_per_file: int = 50
    max_file_size_bytes: int = 10 * 1024 * 1024  # 10 MB
    max_tokens_per_file: int = 100_000

    # Image storage
    images_dir: str = "exports/images"

    model_config = {"env_prefix": "DOCEXTRACTOR_", "case_sensitive": False}


settings = Settings()
