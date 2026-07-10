# DocExtractor

> Extract complete product documentation from vendor websites, preserve the original structure, and export it for offline use.

DocExtractor is a full-stack application that scrapes product documentation from vendor sites (using [Firecrawl](https://firecrawl.dev) backed by Browserless), stores articles with full metadata in PostgreSQL, tracks historical versions with diff views, and exports everything as Markdown or PDF — with optional splitting by article count, file size, or token count.

---

## Features

- **Web scraping with structure preservation** — Discovers the table of contents (TOC) from a documentation site, extracts every article in order, downloads images locally, and stores everything with source URLs and timestamps.
- **20+ documentation platform profiles** — Built-in adapters for Docusaurus, MkDocs, Sphinx, GitBook, ReadTheDocs, Intercom, Zendesk, Freshdesk, HelpJuice, Confluence, Document360, Flare (HTML5 + WebHelp), Oxygen WebHelp, Salesforce, Fern, RSPress, DocFX, Zoomin, and more. A generic sitemap fallback handles unrecognized platforms. An optional LLM fallback can auto-derive a profile for completely unknown sites.
- **PDF source support** — Upload PDF manuals and convert them to Markdown via [Docling](https://github.com/docling-project/docling-serve) (with VLM escalation for complex layouts) or an in-process PyMuPDF fallback.
- **Incremental extraction** — After the initial full run, subsequent runs only fetch changed pages. Historical versions are kept with side-by-side diff views and a consolidated changelog.
- **Scheduled extraction** — Define recurring jobs (interval, daily, weekly, monthly, or cron expressions) to keep documentation archives up to date automatically.
- **Flexible export** — Export full or partial documentation as Markdown or PDF. Split by article count, file size, or token budget. Articles are never split across files.
- **Authenticated scraping** — Store login credentials/cookies for sites behind auth walls. Sessions are encrypted at rest (Fernet). Supports login scripts for complex multi-step authentication flows.
- **Authentication & RBAC** — Optional JWT or API-key authentication with three roles (`admin`, `read_write`, `read_only`). OAuth2 support for Google and Okta. Per-vendor permission scoping.
- **Webhooks** — Register outbound webhooks to receive content-change notifications with HMAC signing and automatic retries.
- **Dashboard** — Overview of sources, health scores, and recent activity.
- **Kubernetes-ready** — Helm chart included for deployment on K3s/k8s with Traefik ingress.

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | FastAPI, SQLAlchemy 2 (async), PostgreSQL, Alembic, Pydantic v2 |
| Scraping | Firecrawl, Browserless, httpx (raw HTTP), Docling (PDF) |
| Frontend | React 19, TypeScript, Vite, Axios |
| Export | Markdown (markdownify), PDF (WeasyPrint) |
| Auth | JWT (PyJWT), API keys, OAuth2 (Google, Okta), bcrypt |
| Infra | Docker Compose, Helm (K3s/k8s), GitHub Actions CI |

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                     Frontend (React)                     │
│              Vite dev / nginx in production              │
└────────────────────────┬────────────────────────────────┘
                         │ /api/*
┌────────────────────────▼────────────────────────────────┐
│                   Backend (FastAPI)                      │
│  ┌─────────┐ ┌──────────┐ ┌──────────┐ ┌─────────────┐  │
│  │ Routes  │ │ Services │ │  Auth    │ │  Profiles   │  │
│  │ (REST)  │ │ (Extract)│ │ Middleware│ │ (20+ sites) │  │
│  └────┬────┘ └────┬─────┘ └──────────┘ └─────────────┘  │
│       │           │                                      │
│  ┌────▼───────────▼──────────────────────────────────┐  │
│  │              PostgreSQL (async)                    │  │
│  └───────────────────────────────────────────────────┘  │
└──────────┬──────────────────────────────────┬───────────┘
           │                                  │
    ┌──────▼──────┐                   ┌───────▼───────┐
    │   Worker    │                   │  Scheduler    │
    │ (extraction │                   │ (cron tick +  │
    │  + export)  │                   │  dead-run     │
    │             │                   │  reaping)     │
    └──────┬──────┘                   └───────────────┘
           │
    ┌──────▼──────┐  ┌──────────┐  ┌──────────────┐
    │  Firecrawl  │  │Browserless│  │Docling-serve │
    │ (scraping)  │  │ (JS render)│  │ (PDF → MD)   │
    └─────────────┘  └──────────┘  └──────────────┘
```

**Four processes:**
- **Backend** (`uvicorn`) — serves the REST API and frontend assets
- **Worker** (`python -m app.worker`) — claims and executes extraction runs and export jobs
- **Scheduler** (`python -m app.scheduler`) — enqueues due jobs and reaps dead runs (single replica, advisory-locked)
- **Frontend** — static SPA served by nginx (production) or Vite dev server

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for a detailed breakdown.

---

## Quick Start

### Prerequisites

- Python 3.13+
- Node.js 20+
- PostgreSQL 16+
- A Firecrawl instance (local or remote)

### Option 1: Docker Compose (recommended)

```bash
git clone https://github.com/carev01/doc-extractor.git
cd doc-extractor

# Start PostgreSQL, backend, worker, scheduler, and frontend
docker compose up -d

# The app is available at http://localhost:3000
# The API is at http://localhost:8000
```

Docker Compose handles database migrations, volume mounts for exports/media, and inter-service networking. See [docker-compose.yml](docker-compose.yml) for the full configuration.

### Option 2: Local Development

#### Backend

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env — at minimum set DOCEXTRACTOR_DATABASE_URL and DOCEXTRACTOR_FIRECRAWL_API_URL

# Run database migrations
alembic upgrade head

# Start the dev server (auto-creates tables on startup)
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

#### Worker (separate terminal)

```bash
cd backend
source .venv/bin/activate
python -m app.worker
```

#### Scheduler (separate terminal)

```bash
cd backend
source .venv/bin/activate
python -m app.scheduler
```

#### Frontend (separate terminal)

```bash
cd frontend
npm install
npm run dev
# Open http://localhost:5173
```

The Vite dev server proxies `/api/*` requests to `http://localhost:8000`.

---

## Configuration

All backend settings are loaded from environment variables with the `DOCEXTRACTOR_` prefix (via [pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/)). See [backend/.env.example](backend/.env.example) for the full list.

### Key Settings

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DOCEXTRACTOR_DATABASE_URL` | ✅ | — | Async PostgreSQL URL (`postgresql+asyncpg://...`) |
| `DOCEXTRACTOR_DATABASE_URL_SYNC` | ✅ | — | Sync PostgreSQL URL for Alembic/tests (`postgresql+psycopg2://...`) |
| `DOCEXTRACTOR_FIRECRAWL_API_URL` | ✅ | — | Firecrawl base URL |
| `DOCEXTRACTOR_FIRECRAWL_API_KEY` | | `""` | Firecrawl API key (not needed for local instance) |
| `DOCEXTRACTOR_WEBHOOK_BASE_URL` | | `""` | URL Firecrawl calls back for per-page events. Empty = polling. |
| `DOCEXTRACTOR_AUTH_JWT_SECRET` | | `""` | JWT signing key. **Empty = auth disabled (dev only).** Generate with `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `DOCEXTRACTOR_SECRET_KEY` | | `""` | Fernet key for encrypting auth realm credentials. Generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `DOCEXTRACTOR_EXPORT_DIR` | | `exports` | Where generated exports are written |
| `DOCEXTRACTOR_MEDIA_DIR` | | `media` | Where article images are stored |
| `DOCEXTRACTOR_LLM_FALLBACK_ENABLED` | | `false` | Enable LLM-based profile derivation for unknown sites |
| `DOCEXTRACTOR_DOCLING_SERVE_URL` | | `http://docling.home.lan` | Docling-serve URL for PDF conversion |

---

## API Overview

The REST API is served at `/api/*`. Key endpoints:

| Area | Endpoints | Description |
|------|-----------|-------------|
| **Vendors** | `GET/POST/PATCH/DELETE /api/vendors` | Manage documentation vendors |
| **Products** | `GET/POST/PATCH/DELETE /api/products` | Manage products under vendors |
| **Sources** | `GET/POST/PATCH/DELETE /api/sources` | Manage documentation sources (web or PDF) |
| **Extraction** | `POST /api/extraction/trigger/{source_id}`, `GET /api/extraction/runs` | Trigger and monitor extraction runs |
| **Articles** | `GET /api/articles`, `GET /api/articles/{id}` | Search and read extracted articles |
| **Export** | `POST /api/export`, `GET /api/export/download/{id}` | Create and download exports |
| **Jobs** | `GET/POST/PATCH/DELETE /api/jobs` | Schedule recurring extraction jobs |
| **Auth** | `POST /api/auth/login`, `POST /api/auth/register`, `GET /api/auth/me` | Authentication and API key management |
| **Webhooks** | `GET/POST/PATCH/DELETE /api/webhooks` | Outbound webhook configuration |
| **Dashboard** | `GET /api/dashboard/sources` | Health scores and overview |
| **Profiles** | `GET /api/profiles` | List available extraction profiles |

Full API reference: [docs/API.md](docs/API.md)

Interactive API docs are available at `/docs` (Swagger UI) when the backend is running.

---

## Documentation Platform Profiles

DocExtractor auto-detects the documentation platform and applies the appropriate scraping strategy:

| Profile | Platform | Strategy |
|---------|----------|----------|
| `docusaurus` | Docusaurus | Sitemap + sidebar parsing |
| `mkdocs` | MkDocs / Material | Nav tree scraping |
| `sphinx` | Sphinx / ReadTheDocs | TOC tree parsing |
| `gitbook` | GitBook | Sidebar TOC |
| `confluence` | Atlassian Confluence | REST API + space tree |
| `intercom` | Intercom Articles | API-based collection |
| `zendesk` | Zendesk Help Center | Category/article API |
| `freshdesk` | Freshdesk Solutions | Category tree |
| `helpjuice` | HelpJuice | Topic tree |
| `document360` | Document360 | Category tree API |
| `flare_html5` | MadCap Flare (HTML5) | TOC + browse sequence |
| `flare_webhelp` | MadCap Flare (WebHelp) | Skin-based TOC |
| `oxygen_webhelp` | Oxygen XML WebHelp | TOC index parsing |
| `salesforce` | Salesforce Help | Browserless-rendered LWC sidebar |
| `fern` | Fern | API docs sidebar |
| `rspress` | RSPress | Sidebar navigation |
| `docfx` | Microsoft DocFX | TOC + manifest |
| `zoomin` | Zoomin | Context-based TOC |
| `devsite` | Google DevSite | Nav + sitemap |
| `dita_api` | DITA-based CMS | DITA map parsing |
| `generic` | Fallback | Sitemap + heuristic TOC |
| `llm` | LLM-derived | Auto-generated profile for unknown sites (opt-in) |

---

## Deployment

### Docker Compose

```bash
docker compose up -d
```

Services: `postgres`, `backend`, `worker`, `scheduler`, `frontend`. Volumes: `postgres_data`, `exports_data`, `media_data`.

### Kubernetes (K3s)

```bash
kubectl create namespace docextractor

helm upgrade --install docextractor deploy/helm/docextractor \
  --namespace docextractor \
  -f deploy/helm/docextractor/values-homelab.yaml \
  --set postgres.password='<db-password>' \
  --set firecrawl.apiKey='<firecrawl-key>'
```

See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) and [deploy/README.md](deploy/README.md) for full details.

---

## Development

### Running Tests

```bash
cd backend

# All tests (requires PostgreSQL with docextractor_test database)
pytest

# Single file
pytest tests/test_versioning.py -v

# Single test
pytest tests/test_defects.py::test_defect2_firecrawl_unavailable_raises -v
```

Tests use a synchronous PostgreSQL connection (`psycopg2`) and the `docextractor_test` database. The CI pipeline creates this database automatically.

### Frontend

```bash
cd frontend

npm run dev      # Dev server
npm run build    # Type-check + production build
npm run lint     # ESLint
```

### Database Migrations

```bash
cd backend
alembic upgrade head                    # Apply all migrations
alembic revision --autogenerate -m "Add new table"  # Generate a new migration
```

### Key Invariants

- **All models must be imported in `app/models/__init__.py`** so `Base.metadata` is populated before `create_all` runs on startup.
- **Extraction uses pre-created run IDs** — the route creates the `ExtractionRun` row and passes its ID to the background task. Never create a second run row.
- **Split never breaks articles** — `ExportEngine._split_articles` guarantees an individual article is never split across output files.
- **Tests use sync DB** — `tests/` use `psycopg2` to avoid asyncpg/pytest-asyncio event-loop conflicts.

---

## Project Structure

```
doc-extractor/
├── backend/
│   ├── app/
│   │   ├── core/           # Database, config, auth middleware, security
│   │   ├── models/         # SQLAlchemy ORM models (17 tables)
│   │   ├── schemas/        # Pydantic request/response schemas
│   │   ├── routes/         # FastAPI routers (12 route files)
│   │   ├── services/       # Business logic
│   │   │   ├── firecrawl.py     # Core extraction engine
│   │   │   ├── exporter.py      # Markdown/PDF export engine
│   │   │   ├── versioning.py    # Article version tracking + diffing
│   │   │   ├── profiles/        # 20+ platform-specific scrapers
│   │   │   ├── auth/            # Login scripts + realm management
│   │   │   ├── pdf_convert.py   # Docling/PyMuPDF PDF conversion
│   │   │   ├── webhook_dispatcher.py
│   │   │   └── ...
│   │   ├── main.py         # FastAPI app entry point
│   │   ├── worker.py       # Background extraction/export worker
│   │   └── scheduler.py    # Cron job scheduler
│   ├── alembic/            # Database migrations
│   ├── tests/              # 142 test files
│   ├── requirements.txt
│   └── Dockerfile
├── frontend/
│   ├── src/
│   │   ├── api/            # Axios API client
│   │   ├── components/     # React components
│   │   ├── views/          # Page-level views
│   │   ├── types/          # TypeScript type definitions
│   │   ├── App.tsx         # Root SPA component
│   │   └── App.css         # Global styles
│   ├── nginx.conf          # Production nginx config
│   └── Dockerfile
├── deploy/
│   ├── helm/               # Helm chart for K3s/k8s
│   └── rendered/           # Pre-rendered manifests
├── docs/                   # Additional documentation
├── postgres/               # DB init scripts
├── docker-compose.yml
├── .github/workflows/ci.yml
└── CLAUDE.md               # AI agent coding guide
```

---

## CI/CD

GitHub Actions ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)):

1. **Backend tests** — PostgreSQL 16 service, Alembic migrations, pytest on every PR and push to `main`
2. **Image build & push** — On `main` and tags, builds `ghcr.io/carev01/doc-extractor-backend` and `ghcr.io/carev01/doc-extractor-frontend` with SHA and `latest` tags

---

## License

Private project. All rights reserved.

---

## Related Documentation

- [Architecture](docs/ARCHITECTURE.md) — Detailed system design and data flow
- [API Reference](docs/API.md) — Full REST API documentation
- [Deployment Guide](docs/DEPLOYMENT.md) — Docker Compose and Kubernetes deployment
- [Contributing](docs/CONTRIBUTING.md) — Development workflow and conventions
- [CLAUDE.md](CLAUDE.md) — AI agent coding guide for this repository