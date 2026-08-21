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
- `app/services/image_describe.py` — opt-in VLM image-description enrichment (`enrich_run_images`): a post-scrape phase that selects meaningful images, describes them (cached by `bytes_sha256` in `image_descriptions`), injects `> **Figure:**` captions into `content_markdown`, and emits an `updated` outbox row. Reuses the OpenRouter VLM pattern from `pdf_escalate.py`.
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
- **Image enrichment never touches the Article's `content_hash`**: `content_hash` is a raw-scrape change-detection fingerprint (already decoupled from the served `content_markdown`). `enrich_run_images` edits `content_markdown` + `ArticleImage` fields and emits an `updated` row, but must NOT recompute the Article's `content_hash` (that would break the unchanged-page fast path and cause phantom deltas). No-churn is enforced by the `is_meaningful IS NULL OR description IS NULL` candidate predicate; `ArticleImage.is_meaningful` is a nullable tri-state (`NULL` = unevaluated), never defaulted. NOTE: the **delta-feed record's** `content_hash` is separately computed at serve time as `sha256(content_markdown)` (in `delta_feed._content_record`) so consumers can detect enrichment updates — don't conflate it with the Article's fingerprint.
- **A profile can override the raw-fetch User-Agent (`raw_user_agent`)**: some edges reject the default browser UA specifically — `www.ibm.com` (`dita_api`) hard-403s it on the toc API, every topic body *and* every image, while allowing a `curl/<ver>` client token. A profile declaring `raw_user_agent` gets it applied to all three: TOC (`Scraper(user_agent=...)`), content (`fetch_raw`), and images (`process_article_result(image_user_agent=...)`). Only the raw_http path passes the image UA, which is sufficient because `content_engine="raw_http"` is routed unconditionally by `_select_content_path` — a future profile pairing `raw_user_agent` with another engine must thread it through the Browserless/batch paths too. Note the failure was *silent*: `build_toc` swallowed the 403, an empty TOC became a synthetic 1-page "Index", and the TOC-collapse guard then blamed "Firecrawl/Browserless" — services that path never touches. A sub-second run duration is the tell-tale.
- **Caption injection must match every markdown image form**: `inject_caption` locates `![alt](path)` *and* `![alt](path "title")` (markdownify emits the title form for any `<img title>` — AvePoint, Securiti, Veeam Help Center set it on every screenshot) plus the `<path>` form. A reference the matcher misses fails silently and permanently: the description is stored, the image leaves the `description IS NULL` backlog (so the source reads "n/n described"), and no caption ever reaches the content — this shipped broken and cost 8,689 captions across 25 sources. `repair_missing_captions` (first phase of `enrich_run_images`) heals such rows from their stored descriptions, so any new matcher gap self-repairs once fixed; keep it write-free when nothing changes, or every run emits phantom `updated` deltas.
- **Three run kinds** (`worker.py` dispatches on `run.kind`): `extract` (`extract_source`, full scrape + enrichment), `escalate` (`retry_escalation_run`, PDF VLM re-conversion, no scrape), `enrich` (`enrich_source_run`, image-enrichment only, drain-all, no scrape — the `POST /api/extraction/enrich/{source_id}` action). Any non-scrape run that mutates content still commits a `run_start` floor and fires `extraction_complete`.
- **TOC-collapse guard baseline**: the guard compares a rebuilt TOC against `min(live articles, last COMPLETED extract run's articles_total)` — never the live count alone, which a past duplication bug can inflate (Arcserve: 518 live rows for 259 real pages made every healthy run abort). Override per run with `allow_toc_collapse` (the UI's "Extract anyway"); it must stay a per-run column, never a setting.
- **UI control gating mirrors `Principal`, never replaces it**: `GET /api/auth/my-access` reports the caller's own `see_all`/effective role/per-vendor grants, and `frontend/src/access.ts` turns that into `canWriteVendor()` / `canManageVendors` / `isAdmin` so the UI hides controls that could only 403 (a read-only vendor shows status + run history and stays clickable to the doc viewer; nothing more). This is presentation only — every mutating route still authorizes independently. Two asymmetries to keep in mind when adding controls: vendor create/rename/delete and CSV import are **admin-only** (`require_admin`), not per-vendor, so they gate on the role; and `/api/jobs` + `/api/auth-realms` are admin-only at the *router* level, so the Jobs and Logins views are hidden for non-admins rather than scoped (a job groups sources across vendors, so per-vendor scoping is a redesign, not a filter). When adding a route, keep `access.ts` in step with `app/core/authz.py`.
- **Frontend type-checking needs `tsc -b`**: `frontend/tsconfig.json` is a solution file (`"files": []` + project references), so a bare `npx tsc --noEmit` type-checks **nothing** and exits 0. Use `npm run build` (which runs `tsc -b`) or `npx tsc -b`.
- **Split never breaks articles**: `ExportEngine._split_articles` guarantees an individual article is never split across output files; a file that would exceed the limit is still written as a single-article file.
- **Tests use synchronous DB**: `tests/` use `psycopg2` + sync `Session` to avoid asyncpg/pytest-asyncio event-loop conflicts. Async routes are tested via `httpx.AsyncClient` if needed.
