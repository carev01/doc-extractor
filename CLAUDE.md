# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

DocExtractor extracts complete product documentation from vendor websites, preserving TOC structure, and exports it as Markdown (with optional splitting by article count, file size, or token count) for offline use. It uses a local [Firecrawl](https://firecrawl.dev) instance for web scraping.

## Project Goals

Extract complete product documentation/user guides (including linked images) from specified vendors/products, preserving the original TOC ordering and structure and individual pages/articles with metadata such as the source url and last update timestamp, store it locally at either a database or object storage (whichever is most appropriate), and allow exporting it in different formats (e.g., markdown, pdf) for offline use. 
The export process should allow partial exports (select chapters/sessions, individual pages/articles, content related to a specific topic) or full. It should also allow splitting the resulting files based on file size, number of articles/pages, or maximum tokens. When splitting files, it should never split a single source page/article.
It must provide an UI to allow users to add vendor documentation urls to be fetched, to trigger manual extraction and to schedule recurrent runs. 
After the initial full run is complete, it must use offer efficient incremental runs to capture changes over time. It must keep the historical versions and allow users to visualize them side-by-side with the current version, as well as keeping a consolidated changelog.
It must offer an API to enable programmatic consumption of the content.

## Additional context

- Firecrawl api available at http://firecrawl.k3s.home.lan (no API Key required)
- Firecrawl is wired internally to a browserless.io local instance that enhances its playwright engine with browserless' stealth capabilities

## Architecture

Full-stack app: FastAPI backend + React/TypeScript frontend. The two are separate projects under `backend/` and `frontend/`.

### Backend (`backend/`)

**Stack:** FastAPI, SQLAlchemy (async via asyncpg), PostgreSQL, Alembic, Pydantic v2, httpx, BeautifulSoup4, markdownify.

**Layer structure:**
- `app/core/` — database engine/session (`database.py`) and settings (`config.py`)
- `app/models/` — SQLAlchemy ORM models. **All models must be imported in `app/models/__init__.py`** so `Base.metadata` is populated before `create_all` runs on startup.
- `app/schemas/` — Pydantic request/response schemas
- `app/routes/` — FastAPI routers (vendors, products, sources, extraction, articles, export, jobs, webhooks, auth)
- `app/services/firecrawl.py` — core extraction engine; `app/services/exporter.py` — markdown export engine; `app/services/webhook_dispatcher.py` — outbound webhook dispatch with HMAC signing and retry
- `app/services/change_log.py` — writes `content_changes` outbox rows (add/update/remove + per-run `run_start` floor); `app/services/delta_feed.py` — streaming JSONL delta feed (`GET /api/articles/delta`) with the gap-free safe-ceiling logic. The outbox powers programmatic incremental sync (e.g. a downstream graph-RAG indexer).
- `app/core/auth_middleware.py` + `app/core/dependencies.py` + `app/core/security.py` — API auth (API keys + OAuth2/JWT with RBAC)
- `exports/` — generated markdown files written here (one subdirectory per export UUID)

**Authentication (opt-in):** disabled by default; enabled by setting `DOCEXTRACTOR_AUTH_JWT_SECRET`. `AuthMiddleware` is the single enforcement point — it authenticates every `/api/` request (X-API-Key or Bearer JWT), stashes `request.state.user`/`effective_role`, and enforces method-based RBAC (safe methods need `read_only`, mutations need `read_write`). FastAPI deps in `dependencies.py` read that state; they don't re-validate. The first registered user bootstraps as `admin`; afterwards `/api/auth/register` is admin-only. Exempt paths: health, `/api/auth/{login,register,refresh,status,oauth/*}`, and the Firecrawl `/api/extraction/webhook/*` callback.

**Extraction flow:** `POST /api/extraction/trigger/{source_id}` creates an `ExtractionRun` row synchronously then dispatches `_run_extraction_background` as a FastAPI `BackgroundTask`. The background task calls `FirecrawlService.extract_source(db, source_id, run_id=run_id)` — the `run_id` must be passed so the service updates the pre-existing run row rather than creating a new one (otherwise the original run is orphaned with status `running`).

**Settings** are loaded via `pydantic-settings` with the `DOCEXTRACTOR_` prefix (e.g. `DOCEXTRACTOR_DATABASE_URL`). Override in `backend/.env`.

**DB defaults:** `postgresql+asyncpg://docextractor:docextractor_dev@localhost:5432/docextractor`. Tests use `docextractor_test` database.

### Frontend (`frontend/`)

**Stack:** React 19, TypeScript, Vite, Axios.

Single-page app with three views (`vendors` → `sources` → `export`) managed by local state in `App.tsx`. All API calls go through `src/api/client.ts`. Types are in `src/types/index.ts`.

The frontend dev server proxies to `http://localhost:8000` (backend). CORS is whitelisted for `localhost:5173` and `localhost:3000`.

## Commands

### Backend

```bash
cd backend

# Install dependencies
pip install -r requirements.txt

# Run dev server (auto-creates tables on startup)
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# Run all tests
pytest

# Run a single test file
pytest tests/test_integration.py -v

# Run a single test
pytest tests/test_defects.py::test_defect2_firecrawl_unavailable_raises -v

# DB migrations (Alembic)
alembic upgrade head
alembic revision --autogenerate -m "description"
```

### Frontend

```bash
cd frontend

# Install dependencies
npm install

# Run dev server (http://localhost:5173)
npm run dev

# Type-check + build
npm run build

# Lint
npm run lint
```

## Key invariants

- **Models must be imported before `create_all`**: `app/main.py` imports `app.models` (not individual models) at startup for this reason. Adding a new model file requires adding it to `app/models/__init__.py`.
- **Extraction uses pre-created run IDs**: The route creates the `ExtractionRun` row and passes its `id` into the background task, so there is never a second run row created. Always pass `run_id` when calling `firecrawl_service.extract_source`.
- **Firecrawl fast-fail**: `FirecrawlService._check_available()` uses a 5s connect timeout to fail quickly when the Firecrawl service is unavailable, instead of hanging for the 300s read timeout.
- **Delta-feed outbox is transactional + gap-free**: every add/update/remove appends a `content_changes` row via `change_log` in the mutation's *own* transaction (never a separate commit). The feed (`delta_feed.py`) serves only below the `_safe_ceiling` (lowest `id` of any active run), and every run — including the PDF `kind="escalate"` retry in `pdf_import.py` — commits a `run_start` sentinel before touching articles. Any new path that mutates article content must also write a `content_changes` row, or that change won't reach the feed.
- **Split never breaks articles**: `ExportEngine._split_articles` guarantees an individual article is never split across output files; a file that would exceed the limit is still written as a single-article file.
- **Tests use synchronous DB**: `tests/` use `psycopg2` + sync `Session` to avoid asyncpg/pytest-asyncio event-loop conflicts. Async routes are tested via `httpx.AsyncClient` if needed.
