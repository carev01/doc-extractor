# DocExtractor — Architecture

## Overview

DocExtractor is a full-stack documentation extraction platform. It scrapes product
documentation from vendor websites (or PDFs), stores it in PostgreSQL with full
metadata and version history, and exports it as Markdown or PDF.

## Process Model

Four long-running processes work together:

### 1. Backend (`uvicorn app.main:app`)

Serves the REST API and static media files. On startup, it:
- Creates database tables if they don't exist (dev convenience; production uses Alembic)
- Verifies that encrypted auth realms have a secret key configured
- Mounts `/media` for serving canonical article images

The backend does **not** run extractions itself — it creates `ExtractionRun` rows
and the worker picks them up. Export jobs are similarly dispatched to the worker.

### 2. Worker (`python -m app.worker`)

A single-replica background process that:
- Polls for pending extraction runs (every 2 seconds)
- Claims a run via `SELECT ... FOR UPDATE SKIP LOCKED` (safe concurrency)
- Calls `FirecrawlService.extract_source()` with the pre-created run ID
- Sends heartbeat updates every 15 seconds
- Flushes log buffers every 10 seconds
- Also claims and executes pending export jobs
- Runs maintenance sweeps (export retention, media GC) on idle cycles

### 3. Scheduler (`python -m app.scheduler`)

A single-replica process (advisory-locked via `pg_try_advisory_lock`) that runs
every 30 seconds:
- Reaps dead runs (stale heartbeat → marked as failed)
- Enqueues due scheduled jobs
- Cancels runs that have been paused/cancelled by the user

### 4. Frontend

React SPA served by nginx in production (proxies `/api/*` to the backend) or by
Vite dev server in development (proxy to `http://localhost:8000`).

## Data Flow

### Extraction Flow

```
User triggers extraction (UI or API)
    │
    ▼
POST /api/extraction/trigger/{source_id}
    │
    ├── Creates ExtractionRun row (status=pending)
    └── Returns run_id immediately
    │
    ▼
Worker claims run from queue
    │
    ├── Updates status to "running"
    ├── Sends heartbeats
    ├── Commits a run_start floor row into content_changes (delta-feed watermark)
    │
    ├── Phase 1: TOC Discovery
    │   ├── Detect platform (or use configured profile)
    │   ├── Fetch and parse the table of contents
    │   ├── Store TOC entries (tree structure)
    │   └── Checkpoint for resume
    │
    ├── Phase 2: Content Scraping
    │   ├── For each article URL in TOC order:
    │   │   ├── Fetch page via Firecrawl/Browserless/raw HTTP
    │   │   ├── Convert HTML → Markdown
    │   │   ├── Download and store images locally
    │   │   ├── Compare with previous version (if exists)
    │   │   ├── Store new/updated article + version
    │   │   ├── Append added/updated row to content_changes (same transaction)
    │   │   └── Update progress counters
    │   ├── Reconcile removals → append removed rows to content_changes
    │   └── Handle resume from checkpoint
    │
    └── Phase 3 (PDF sources): PDF Acquire → Convert → Split → VLM Escalate
        └── VLM escalation (incl. kind="escalate" retry) also appends
            updated rows so improved content reaches the delta feed
    │
    ▼
Run completes (status=completed or failed)
    │
    ▼
Webhooks dispatched (extraction_complete carries a delta summary)
```

### Export Flow

```
User requests export (UI or API)
    │
    ▼
POST /api/export
    │
    ├── Creates ExportJob row
    └── Returns job_id
    │
    ▼
Worker claims export job
    │
    ├── Fetches articles (full or partial by chapter/article/topic)
    ├── Assembles Markdown
    ├── Splits by article count / file size / token budget
    │   (articles are never split across files)
    ├── For PDF: renders Markdown → HTML → WeasyPrint
    └── Writes files to exports/{export_id}/
    │
    ▼
User downloads via GET /api/export/download/{export_id}
```

### Versioning Flow

When an article is re-extracted:
1. Compare new content hash with the latest version
2. If changed → create `ArticleVersion` row with the old content
3. Update the `Article` with new content
4. Diff is computed on-demand (line-level, rendered in the UI)
5. Changelog entries are generated per source

### Delta Feed

The delta feed (`GET /api/articles/delta`) lets a downstream consumer — for example a
graph-RAG indexer — bootstrap from the full corpus and then incrementally apply
additions, updates, and removals. It is backed by an **append-only outbox**,
`content_changes`, with a `BIGSERIAL id` that is the feed's monotonic **watermark**.

- **Write path**: every article add / update / remove appends one `content_changes` row
  **in the same database transaction** as the mutation (`app/services/change_log.py`,
  called from `firecrawl.py` and `pdf_import.py`), so the feed can never disagree with
  the stored data.
- **Serve path** (`app/services/delta_feed.py`): rows are streamed as JSONL ordered by
  `id`. `added`/`updated` become content records (joined to the live article + provenance);
  `removed` become tombstones that survive article/source hard-deletion (FKs are
  `ON DELETE SET NULL` and `topic_key` is copied onto the row). Omitting the `since` cursor
  yields a full bootstrap snapshot.
- **Gap-free under concurrent runs**: ordering is by `id` alone. The feed serves only below
  a **safe ceiling** — the lowest `id` belonging to any still-active run — so a slow run's
  rows are withheld until it terminalizes and can't be skipped by a faster concurrent run.
  Because a `BIGSERIAL id` is invisible until commit, each run also commits a **`run_start`
  sentinel** row before processing any article, giving every active run a committed floor in
  id space and closing the flush→commit window (relevant only for multi-replica workers;
  single-replica runs never overlap).
- **Trigger**: the `extraction_complete` webhook carries a `delta` summary (counts + watermark)
  so a consumer knows when to pull. The feed — not the webhook — is the reliable channel, so a
  missed delivery self-heals on the next pull.

## Database Schema

18 models across the following domains:

| Domain | Models | Purpose |
|--------|--------|---------|
| Hierarchy | `Vendor`, `Product`, `DocumentationSource` | Vendor → Product → Source tree |
| Content | `Article`, `TOCEntry`, `Image` | Extracted articles, TOC structure, images |
| Versioning | `ArticleVersion` | Historical article versions for diffing |
| Delta Feed | `ContentChange` | Append-only outbox (watermark) powering the delta feed |
| Extraction | `ExtractionRun`, `TOCCheckpoint` | Run tracking and resume checkpoints |
| Export | `ExportJob` | Export job queue |
| Scheduling | `Job`, `JobRun` | Recurring extraction schedules |
| Auth | `User`, `APIKey`, `UserVendorPermission` | Users, keys, per-vendor RBAC |
| Auth Realms | `AuthRealm` | Stored credentials for authenticated scraping |
| Webhooks | `Webhook` | Outbound webhook configuration |

### Key Relationships

```
Vendor 1──* Product 1──* DocumentationSource 1──* Article
                                    │                   │
                                    1                   1
                                    │                   │
                                    *                   *
                              ExtractionRun       ArticleVersion
                                    │
                                    1
                                    │
                                    *
                               TOCCheckpoint
```

## Extraction Profiles

Profiles are the adapter layer between DocExtractor and the myriad ways
documentation sites structure their content. Each profile implements:

1. **Detection** — Given a URL, is this profile the right one?
2. **TOC Discovery** — How to fetch the table of contents
3. **Content Scraping** — How to fetch and parse individual article pages

The profile registry (`app/services/profiles/registry.py`) auto-detects the
platform from the URL and page structure. If no specific profile matches, the
generic sitemap-based profile is used. With `LLM_FALLBACK_ENABLED=true`, an LLM
can analyze the site and derive a custom profile spec.

### Scraping Engines

| Engine | When | How |
|--------|------|-----|
| **Firecrawl** | Default | HTTP fetch + HTML serialization via local Firecrawl |
| **Browserless** | JS-rendered SPAs (Salesforce, etc.) | Real Chrome with JS execution |
| **Raw HTTP** | Static sites | Direct httpx GET + local HTML parsing |

### PDF Pipeline

For PDF sources, a multi-phase conversion pipeline runs:

1. **PDF Acquire** — Copy/upload the PDF to local storage
2. **PDF Convert** — Docling-serve converts pages to Markdown (batched by `pdf_convert_batch_pages`)
3. **PDF Split** — Large PDFs are split into article-sized chunks
4. **VLM Escalate** — Pages with poor conversion quality are re-processed through a VLM (vision-language model) for better layout understanding

## Authentication

Authentication is **opt-in**: disabled by default, enabled by setting
`DOCEXTRACTOR_AUTH_JWT_SECRET`.

### Auth Flow

```
Request → AuthMiddleware
    │
    ├── Check exempt paths (health, login, register, webhooks)
    ├── Try X-API-Key header → look up key → validate
    ├── Try Bearer JWT → verify signature → extract user
    ├── Stash user + role in request.state
    └── Enforce RBAC:
        ├── Safe methods (GET, HEAD) → need read_only+
        └── Mutations (POST, PUT, PATCH, DELETE) → need read_write+
```

### Roles

| Role | Permissions |
|------|------------|
| `admin` | Everything: user management, all vendors, all operations |
| `read_write` | Create/update/delete within permitted vendors |
| `read_only` | Read access within permitted vendors |

The first registered user becomes `admin`. Afterwards, `/api/auth/register` is
admin-only.

### Per-Vendor Permissions

Admins see all vendors. Non-admin users can be granted per-vendor permissions
(`none`, `read_only`, `read_write`) via the User Management UI.

### OAuth2

Optional OAuth2 login for Google and Okta. Configure client ID/secret and set
`DOCEXTRACTOR_AUTH_OAUTH_REDIRECT_BASE` to the frontend URL.

## External Dependencies

| Service | Purpose | Required? |
|---------|---------|-----------|
| **PostgreSQL 16** | Primary data store | ✅ Yes |
| **Firecrawl** | Web scraping engine | ✅ Yes |
| **Browserless** | JS-rendered content scraping | Optional (for SPAs) |
| **Docling-serve** | PDF → Markdown conversion | Optional (for PDF sources) |
| **OpenRouter / VLM** | Vision-language model for PDF escalation | Optional |
| **LLM provider** | Profile derivation for unknown sites | Optional |

## Design Decisions

- **Async database**: SQLAlchemy async with `asyncpg` for the app; `psycopg2` sync
  for Alembic migrations and tests (avoids pytest-asyncio event-loop conflicts).
- **Background tasks, not Celery**: The worker uses `SELECT FOR UPDATE SKIP LOCKED`
  for queue claiming — no Celery/Redis dependency.
- **Pre-created run IDs**: The route creates the `ExtractionRun` row and passes its
  ID to the background task, so there's never a duplicate or orphaned run.
- **Checkpoint-based resume**: TOC checkpoints allow resuming interrupted extractions
  without re-doing completed work.
- **Transactional outbox for the delta feed**: content changes are recorded in an
  append-only `content_changes` table written in the mutation's own transaction, rather
  than derived at read time. A `BIGSERIAL` id gives a monotonic watermark; a safe-ceiling
  gate plus a per-run `run_start` floor make the feed gap-free even when runs overlap.
- **Images are canonical**: Article images live in `media_dir` (served at `/media`),
  separate from generated exports. Exports rewrite image URLs to relative paths.
- **Export retention**: Generated export directories are purged after
  `export_retention_days` (default 7) and capped at `export_max_total_bytes` (default 3 GiB).