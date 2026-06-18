# Async, Bounded-Memory Export — Design

**Date:** 2026-06-18
**Status:** Approved (design); pending implementation plan
**Goal addressed:** Scale export to thousands of articles. Today export runs synchronously in the HTTP request and loads every selected article's full content into memory before building one giant document — it blocks a web connection for minutes and risks OOM. This moves export onto the existing job queue/worker and rebuilds generation to use bounded memory.

This is **sub-project 1** of a scaling effort. Sub-project 2 (read-path scaling — browse/TOC pagination + frontend tree virtualization) is a separate later spec. A live stress test of a large source is explicitly deferred (the user will run it once the foundations land).

## Summary

Exports become asynchronous jobs on the same Postgres-backed queue the extraction worker already drains, and the export engine is split into a lightweight **plan pass** (decide grouping from stored metadata only) and a memory-bounded **render pass** (process one chapter/group at a time). PDF renders per chapter and merges with `pypdf`, so peak memory is ~one chapter regardless of source size.

## Decisions (locked during brainstorming)

| Decision | Choice | Rationale |
|---|---|---|
| Scope | Async + bounded-memory export now; read-path scaling later; no stress test now | Tackle the acute bottleneck first; user verifies scale later. |
| Job model | Separate `export_jobs` table; the existing worker claims both run and export jobs | Minimal blast radius — `ExtractionRun` and its tests are untouched; reuses the proven claim/heartbeat/reaper machinery. |
| PDF at scale | Per-chapter render + merge (`pypdf`) | Peak memory ≈ one chapter; scales to any size. Tradeoff: PDF is a concatenation of chapter documents (no single global cross-page TOC). |
| Markdown at scale | Stream/write incrementally, one group at a time | Never holds all content in memory. |
| API | `POST /api/export` enqueues + returns a job id; UI polls `GET /api/export/jobs/{id}`; download unchanged | Mirrors the extraction trigger/poll pattern already in the UI. |

## Job model & lifecycle

New `export_jobs` table (mirrors the extraction queue columns):

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | the export job id |
| `source_id` | uuid FK → sources (CASCADE) | |
| `request` | JSONB | the full `ExportRequest` payload |
| `status` | enum `pending\|running\|completed\|failed\|cancelled` | |
| `claimed_by` / `claimed_at` / `heartbeat_at` / `attempts` | as in `extraction_runs` | claim + reaper machinery |
| `export_id` | uuid, nullable | the generated export's id (the on-disk dir + download key) |
| `result` | JSONB, nullable | file info / `zip_filename` / counts (the old `ExportResponse` body) |
| `error_message` | str, nullable | |
| `created_at` / `started_at` / `completed_at` | timestamptz | |

Indexes mirror the run queue: a partial index on `(created_at) WHERE status='PENDING'` for the claim scan. (No one-active-per-source constraint — concurrent exports of one source are allowed.)

**Worker loop:** each iteration claims an extraction run first, else an export job (`claim_next_export`, `FOR UPDATE SKIP LOCKED`, oldest-first, sets RUNNING + `claimed_by`/`heartbeat_at`/`attempts`). Both run under the existing heartbeat task. On success the job is `completed` with `export_id`/`result`; on exception it is `failed` (safety-net) — same shape as run execution.

**Reaper:** the scheduler tick's `reap_stale_runs` gains a sibling `reap_stale_exports` (same heartbeat-cutoff + attempts-cap logic) so a dead worker's export is requeued/failed.

## Bounded-memory generation

The engine splits into two passes:

1. **Plan pass** — load only lightweight per-article metadata: `id, title, sort_order, toc_entry_id, content_size_bytes, estimated_tokens` (no `content_markdown`, no images). The existing split/chapter grouping (`_split_articles` / `_split_by_chapter` / `_chapter_keys`) is computed from these stored columns alone, producing an ordered list of groups where each group is a list of **article ids** (+ the metadata needed for filenames/TOC). This proves we never load content to decide grouping.

2. **Render pass** — iterate one group at a time. For each group, load *only that group's* articles with `content_markdown` + images, render, write the output file, and release the objects before the next group. 
   - **Markdown:** build the group's markdown, rewrite media URLs to relative `images/`, write the `.md` file, copy that group's images. (One group's content in memory at a time.)
   - **PDF:** render the group to a chapter PDF (`render_markdown_to_pdf`, images embedded), accumulate the chapter PDF paths, then `pypdf` **merges** them into the final `<name>.pdf`. Chapter PDFs are temporary and removed after merge. Peak memory ≈ one chapter.

The single-group (no-split) case for PDF is still chunked by **top-level chapter** so a full export of a huge source stays bounded; if a source has no chapter structure, it falls back to fixed-size article batches.

The ZIP bundling and `download` route are unchanged: markdown bundles carry the `.md` file(s) + `images/`; PDF bundles carry the merged `.pdf` (images embedded, no `images/` dir).

## API & polling

- `POST /api/export` — validates the request, creates a `pending` `export_jobs` row, returns `{export_job_id, status:"pending"}`. (The old synchronous `POST /api/export/markdown` is replaced; the worker does the generation.)
- `GET /api/export/jobs/{id}` — returns `{id, status, source_id, export_id, files, zip_filename, error_message}`; `files`/`zip_filename`/`export_id` are populated once `completed`.
- `GET /api/export/download/{export_id}` and `/download/{export_id}/{filename}` — unchanged.

## Frontend

`ExportPanel`: "Generate" POSTs to enqueue, stores the `export_job_id`, and polls `GET /api/export/jobs/{id}` (~2s, like the run-status poll) showing **Queued… / Generating…**. On `completed` it shows the download link (from `export_id`); on `failed` it shows `error_message`. Format/split/selection controls are unchanged. The long-timeout workaround on the export call is removed (the request is now fast — it only enqueues).

## Dependencies

- Add `pypdf` to `requirements.txt` (PDF merge). `weasyprint` / `markdown` already present. No new native libs.

## Testing

- **`export_jobs` queue** (sync DB, mirrors `test_queue`): `enqueue_export` creates a pending job; `claim_next_export` marks running + sets claim fields under `SKIP LOCKED`; `reap_stale_exports` requeues a stale running job and fails it at the attempts cap.
- **Worker** (mirrors `test_worker`): `run_one` claims and executes an enqueued export job to `completed` with `export_id`/`result` set; on a raised generation error the job is `failed`. (No Firecrawl involved.)
- **Bounded generation** (sync DB): the plan pass groups using metadata only (assert no `content_markdown` is loaded for the grouping decision — e.g. by exercising it against articles and checking grouping matches the existing `_split_articles` output); a markdown export streams correct content for a multi-group split; a PDF export of a multi-chapter source produces a merged PDF whose page count equals the sum of the chapter renders and whose magic bytes are `%PDF`.
- **Route**: `POST /api/export` returns a job id and a `pending` job; `GET /api/export/jobs/{id}` round-trips status; a completed job exposes `.pdf`/`.md` files.
- **Existing export behaviour** (`test_integration.py`): markdown/PDF output content is unchanged vs. today (regression) — the `export_sync` path stays available for these tests, now routed through the same two-pass engine.
- **Frontend**: `npm run build` + `npm run lint` clean.

## Out of scope
- Read-path scaling (browse/TOC pagination, frontend virtualization) — sub-project 2.
- A live large-source stress test — user will run it.
- A dedicated export-only worker / queue fairness tuning (single worker claims both; revisit only if exports starve extractions).
- Global cross-page clickable PDF TOC with page numbers (per-chapter merge does not produce one).
- Export retention/cleanup policy (unchanged from today).
