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
    # Export retention — generated export dirs are purged once older than this many
    # days, and the total export footprint is capped (oldest-first eviction) so the
    # exports volume can't fill from accumulation. 0 days disables the age sweep.
    export_retention_days: int = 7
    export_max_total_bytes: int = 3 * 1024 * 1024 * 1024  # 3 GiB

    # Image storage — canonical (source-of-truth) images live in media_dir,
    # kept separate from generated exports/. Served over HTTP at media_url_prefix
    # so the frontend can render them and exports can rewrite to relative paths.
    media_dir: str = "media"
    media_url_prefix: str = "/media"

    # LLM fallback profile — off by default; requires an API key.
    # Set DOCEXTRACTOR_LLM_FALLBACK_ENABLED=true to enable. When enabled,
    # unrecognized sites are analysed by the LLM before falling back to the
    # generic sitemap profile. The derived spec is cached in
    # source.profile_config["llm_spec"] so subsequent runs skip re-derivation.
    #
    # Provider selection:
    #   DOCEXTRACTOR_LLM_PROVIDER   — "anthropic" (default) | "openai"
    #   DOCEXTRACTOR_LLM_BASE_URL   — override endpoint (blank → provider default)
    #   DOCEXTRACTOR_LLM_API_KEY    — API key (Anthropic sk-ant-... or OpenAI sk-...)
    #   DOCEXTRACTOR_LLM_MODEL      — model name (blank → provider default)
    #   DOCEXTRACTOR_LLM_MAX_TOKENS — response token budget for spec derivation.
    #     Reasoning models (e.g. gpt-oss) spend tokens thinking before emitting
    #     the JSON spec, so this needs headroom above the raw spec size.
    llm_fallback_enabled: bool = False
    llm_provider: str = "anthropic"   # "anthropic" | "openai"
    llm_base_url: str = ""            # blank → provider default
    llm_api_key: str = ""
    llm_model: str = ""               # blank → provider default
    llm_max_tokens: int = 2048

    model_config = {
        "env_prefix": "DOCEXTRACTOR_",
        "case_sensitive": False,
        "env_file": ".env",
        "env_file_encoding": "utf-8",
    }


settings = Settings()
