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

    # Browserless — a real Chrome with a JS-execution API, used by profiles that
    # need shadow-DOM-rendered content Firecrawl can't serialize (e.g. Salesforce
    # Help, a Lightning Web Components SPA). The in-cluster service is the default;
    # the token is supplied at deploy (DOCEXTRACTOR_BROWSERLESS_TOKEN).
    browserless_url: str = "http://browserless.browserless.svc.cluster.local:3000"
    browserless_token: str = ""
    # Per-article render budget (ms) and concurrency for browserless content scraping.
    browserless_wait_ms: int = 9000
    browserless_concurrency: int = 4
    # TOC expansion clicks every parent in a lazy sidebar (e.g. a
    # ~9,670-node tree). The page renders once, then each toggle is a cheap
    # in-page click (~200ms), but a very large section still needs a long
    # session. The index build splits into one session per top-level section
    # (each checkpointed for resume), so this caps a single section's walk.
    browserless_toc_timeout_ms: int = 1_800_000  # 30 min

    # raw_http content engine — direct httpx GET + local body scoping for
    # statically-served docs (no Firecrawl/Browserless). Fetches are cheap, so
    # this concurrency is higher than the browserless one. The failure guard
    # aborts a run when too large a fraction of pages fail to fetch/scope (a site
    # that changed structure or started bot-gating) rather than silently storing
    # a partial doc set; min-attempts avoids tripping on tiny sources.
    raw_http_concurrency: int = 8
    raw_http_max_failure_rate: float = 0.3
    raw_http_min_attempts: int = 10

    # TOC-collapse guard — data-loss protection. If a run's rebuilt TOC has fewer
    # scrapable pages than ``toc_collapse_min_ratio`` of the source's currently-live
    # article count (and that prior count is at least ``toc_collapse_min_prior``),
    # TOC discovery almost certainly failed (an overloaded/unavailable
    # Firecrawl/Browserless, an upstream change, a transient empty nav). The run is
    # aborted BEFORE the destructive TOC rebuild + removal reconcile, so good
    # content is never wiped. A genuine large shrink then needs a human re-trigger.
    toc_collapse_min_ratio: float = 0.5
    toc_collapse_min_prior: int = 20

    # PDF source import — uploaded PDFs live on a local volume (a PVC in k8s),
    # mirroring media_dir/export_dir. Uploads larger than the cap are rejected.
    pdf_dir: str = "pdf_uploads"
    pdf_max_upload_bytes: int = 100 * 1024 * 1024  # 100 MiB

    # PDF conversion engine (Layer A) and VLM escalation (Layer B).
    # pdf_converter: "docling" (default, remote docling-serve) | "pymupdf"
    # (in-process fallback engine). docling-serve is consumed over HTTP — no
    # docling/torch is embedded in this image.
    pdf_converter: str = "docling"
    docling_serve_url: str = "http://docling.home.lan"
    docling_serve_api_key: str = ""          # X-Api-Key (env only — .env is tracked)
    docling_serve_timeout: float = 600.0     # per-request read timeout (s)
    docling_serve_poll_interval: float = 3.0  # async convert: status poll cadence (s)
    # How long to keep retrying a transient docling-serve error (e.g. an ingress
    # 502 while the pod restarts) before giving up on the request. Sized to ride
    # out a worker restart so a brief blip doesn't dump a whole document to the
    # pymupdf fallback. Capped by docling_serve_timeout.
    docling_serve_transient_window: float = 120.0
    # If docling-serve restarts mid-conversion it drops its in-memory job
    # registry, so polling the task id returns 404. Resubmit a fresh convert job
    # (up to this many times) rather than abandoning the document to pymupdf.
    docling_serve_max_resubmits: int = 2
    # Large PDFs are converted through docling in page-range batches of this many
    # pages, so docling-serve doesn't load a whole 150+ page doc at once (OOM).
    # A doc with <= this many pages is converted in a single call.
    pdf_convert_batch_pages: int = 80
    # VLM escalation runs through docling-serve's VLM pipeline, pointed at an
    # OpenAI-compatible remote model (OpenRouter). The app forwards the endpoint,
    # bearer key, and model name in the convert request — never calls Anthropic.
    pdf_vlm_escalation_enabled: bool = True
    pdf_vlm_base_url: str = "https://openrouter.ai/api/v1/chat/completions"
    pdf_vlm_api_key: str = ""                 # Bearer key (env only)
    pdf_vlm_model: str = "qwen/qwen3-vl-32b-instruct"
    # Per-run VLM escalation page budget, as a percentage of the document's total
    # page count (scales with document size instead of a fixed cap). e.g. 10.0 =>
    # up to ~10% of pages may be VLM-re-converted per run; rounded up, min 1 page.
    pdf_vlm_max_pages_pct: float = 10.0
    # Circuit breaker: if this many escalation segments fail consecutively (e.g.
    # docling-serve's VLM pipeline is misconfigured or its upstream model is
    # down), stop escalating for the rest of the run instead of hammering the
    # shared service with dozens of doomed conversions.
    pdf_vlm_max_consecutive_failures: int = 5

    # ── Image VLM description (Spec 2, opt-in) ──
    # OpenAI-compatible vision chat-completions endpoint (OpenRouter by default),
    # kept separate from pdf_vlm_* so image and PDF budgets tune independently.
    image_vlm_enabled: bool = False
    image_vlm_base_url: str = "https://openrouter.ai/api/v1/chat/completions"
    image_vlm_api_key: str = ""                 # Bearer key (env only)
    image_vlm_model: str = "qwen/qwen3-vl-32b-instruct"
    image_vlm_max_per_run: int = 100            # budget: max NEW descriptions per run
    image_vlm_max_consecutive_failures: int = 5  # circuit breaker
    image_vlm_max_tokens: int = 300
    # Selection thresholds: images smaller than either are treated as decorative.
    image_min_dimension: int = 100
    image_min_bytes: int = 3072

    # Master key for encrypting credentials/sessions at rest (Fernet, urlsafe
    # base64, 32 bytes). Required only when auth_realm rows exist. Generate with:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    secret_key: str = ""

    # Generic outbound webhook for operator alerts (e.g. a realm session expired
    # mid-run). Blank disables. POSTs JSON {title,message,text,content,...}.
    notify_webhook_url: str = ""

    # User-configured content-change webhooks (see webhook_dispatcher) POST to
    # URLs the operator supplies. Loopback and link-local targets (incl. the
    # 169.254.169.254 cloud-metadata address) are always rejected. Private LAN
    # ranges (10/8, 172.16/12, 192.168/16) are allowed by default because this
    # is an internal tool whose webhook targets (ntfy/Slack relays) usually live
    # on the LAN; set this False to lock delivery to public targets only.
    webhook_allow_private_targets: bool = True

    # ── API authentication ──────────────────────────────────────────────
    # JWT signing key for access/refresh tokens. When EMPTY, authentication is
    # DISABLED (the API is open — dev only). Set it to require auth. Generate:
    #   python -c "import secrets; print(secrets.token_urlsafe(48))"
    auth_jwt_secret: str = ""
    auth_jwt_algorithm: str = "HS256"
    auth_access_token_expire_minutes: int = 30
    auth_refresh_token_expire_days: int = 7

    # OAuth2 providers — set client_id/client_secret to enable each provider.
    # Redirect URL pattern: {base}/api/auth/oauth/{provider}/callback
    auth_oauth_redirect_base: str = "http://localhost:5173"
    auth_google_client_id: str = ""
    auth_google_client_secret: str = ""
    auth_okta_client_id: str = ""
    auth_okta_client_secret: str = ""
    auth_okta_domain: str = ""

    model_config = {
        "env_prefix": "DOCEXTRACTOR_",
        "case_sensitive": False,
        "env_file": ".env",
        "env_file_encoding": "utf-8",
    }


settings = Settings()
