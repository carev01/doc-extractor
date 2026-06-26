# Design: PDF source import + CSV-import relocation

Date: 2026-06-26

Two pieces of work:

1. **Move CSV import** from the Dashboard view to the Vendors view (trivial UI relocation).
2. **PDF sources** — import documentation from a PDF (URL or upload), convert it to
   per-article markdown split on natural content boundaries, and treat it as a
   first-class source with full incremental/versioning parity.

The PDF feature is the substantial one; the CSV move is a small first task.

---

## Part 0 — Move CSV import to the Vendors view

### Problem
`BulkImport` (CSV import of sources) currently opens from the Dashboard header. It
belongs with vendor/product management.

### Change
- `frontend/src/components/Dashboard.tsx`: remove the **Import CSV** button, the
  `showImport` state, and the `<BulkImport>` render.
- `frontend/src/components/VendorList.tsx`: add an **Import CSV** button in the
  view header; reuse the existing `BulkImport` component unchanged; on
  `onImported`, re-fetch the vendor list (inline `listVendors().then(...)`,
  matching the lint-clean effect pattern already in that file).
- No backend change. `BulkImport.tsx` itself is unchanged.

---

## Part 1 — PDF source data model & origin

### Source type discriminator
Add `source_type` to `DocumentationSource`:

- `source_type: Mapped[str]` — `String(16)`, `default="web"`, `server_default="web"`,
  `nullable=False`. Values: `"web"` | `"pdf"`. Existing rows backfill to `"web"`.
- Alembic migration adds the column with server default `"web"`.

### Origin of the PDF
A PDF source records its origin in one of two ways:

- **From URL:** `base_url` holds the PDF's URL. Each run re-downloads it (enables
  incremental diffing).
- **From upload:** the uploaded file is stored on a new local volume as
  `<pdf_dir>/<source_id>.pdf`. `base_url` holds the synthetic marker
  `file://<source_id>.pdf` so the NOT-NULL `base_url` column is satisfied and the
  pipeline knows to read from disk rather than fetch.

A helper `pdf_is_upload(source) -> bool` returns `source.base_url.startswith("file://")`.

### Storage & settings (`app/core/config.py`)
Mirror the existing `media_dir` / `export_dir` local-volume pattern:

- `pdf_dir: str = "pdf_uploads"` — directory for uploaded PDFs (a PVC in k8s).
- `pdf_max_upload_bytes: int = 100 * 1024 * 1024` — reject larger uploads (413).

PDF segmentation may use the LLM fallback, which reuses the **existing** settings:
`llm_provider`, `llm_base_url`, `llm_api_key`, `llm_model`, `llm_max_tokens`,
`llm_fallback_enabled`. No new LLM settings.

---

## Part 2 — Conversion pipeline (`app/services/pdf_import.py`)

A PDF source bypasses the web profile/TOC crawl. `FirecrawlService.extract_source`
branches at the top: when `source.source_type == "pdf"`, it delegates to
`pdf_import.run_pdf_extraction(db, source, run, run_pk)` instead of resolving a web
profile. The pipeline produces the **same** `TOCEntry` + `Article` rows the web path
produces, so export, versioning, browse, and changelog keep working unchanged.

### Units (each independently testable)

**`acquire_pdf(source) -> tuple[bytes, str]`**
- URL source: download `source.base_url` with `httpx` (reuse the service's timeout
  posture); return `(pdf_bytes, sha256_hex)`.
- Upload source: read `<pdf_dir>/<source_id>.pdf`; return `(pdf_bytes, sha256_hex)`.
- Raises a typed `PdfAcquireError` on network/missing-file errors so the run fails
  cleanly with a message.

**`segment_pdf(pdf_bytes) -> list[Segment]`**
A `Segment` is `{title, level, page_start, page_end, path: list[str]}`.
- **Outline-first:** read the embedded outline via PyMuPDF `doc.get_toc(simple=True)`
  (`[level, title, page]` rows). Each entry becomes a segment spanning from its page
  to the page before the next entry at the same-or-higher level; the last entry runs
  to the final page. `level` and order are taken from the outline; `path` is the
  list of ancestor titles (for the topic key).
- **LLM fallback:** when the outline is missing or trivial (0 or 1 entries) and
  `llm_fallback_enabled` is true, extract the document's plain text and ask the LLM
  (same call pattern as `profiles/llm.py::derive_spec`) to return an ordered list of
  `{title, level}` section headings, matched back to their first occurrence in the
  text to derive page ranges. Capped/chunked to respect `llm_max_tokens`.
- **Heuristic fallback:** when no outline and the LLM is disabled/unavailable,
  detect headings by font size/weight (PyMuPDF span metadata); group into segments.
- **Worst case:** a single segment covering the whole document (title = the source
  name). The pipeline never produces zero articles for a non-empty PDF.

**`segment_to_markdown(pdf_bytes, segment) -> str`**
- Convert the segment's page range to markdown with `pymupdf4llm.to_markdown(doc,
  pages=range(...))`. Trim leading/trailing boilerplate (running headers/footers)
  with the existing `sanitize_markdown` step the web path already applies.

**`run_pdf_extraction(db, source, run, run_pk)`** — orchestration:
1. `acquire_pdf`; compute `pdf_hash`.
2. **Fast path:** if a prior run stored the same `pdf_hash` (tracked on the run, see
   below) and articles exist, mark every existing article `unchanged` (bump
   `extracted_at`), set counters, complete. No re-segmentation.
3. Otherwise `segment_pdf` → build the TOC tree (delete-and-rebuild, same as the web
   path) and, per segment, `segment_to_markdown` → upsert the `Article` by
   `topic_key` through the **existing** persist/diff helpers so new/updated/unchanged
   accounting and `ArticleVersion` snapshots happen exactly as for web articles.
4. Reconcile removals (segments gone from the new outline) via the existing
   `_reconcile_removals`.
5. Complete the run (status, counters, `source.last_extracted_at`).

### Topic key (incremental stability)
PDF articles derive `topic_key` from the **outline path slug**, not a URL:
`slugify(path joined by "/")`, e.g. `chapter-1/installation`. A new
`derive_pdf_topic_key(path: list[str]) -> str` lives next to the existing
`derive_topic_key`. Stable titles → stable keys → clean diffs across re-conversions.
`source_url` for a PDF article is `base_url#page=<page_start>` (URL source) or the
file marker + `#page=N` (upload), so the UI has a deep link.

### Recording the PDF hash
Add `pdf_hash: Mapped[str | None]` (`String(64)`) to `ExtractionRun`. A **single**
Alembic migration adds both new columns (`documentation_sources.source_type` and
`extraction_runs.pdf_hash`). The fast path compares the new hash against the most
recent completed run's `pdf_hash` for the source.

---

## Part 3 — Incremental & versioning (full parity)

- **URL PDFs:** manual/scheduled runs re-download, hash, and either fast-path
  (unchanged) or re-convert and diff per `topic_key`. Identical to how web sources
  behave; they ride the existing scheduling/job fan-out unchanged (a PDF source can
  be assigned to a job like any other).
- **Uploaded PDFs:** re-uploading replaces `<pdf_dir>/<source_id>.pdf`; the next run
  sees a new hash and diffs. A re-upload endpoint (`PUT /api/sources/{id}/pdf`)
  overwrites the stored file.
- Versions, changelog, browse change-status, and export all work through the
  existing machinery because PDF articles are ordinary `Article` rows with
  `content_hash`, `topic_key`, `sort_order`, and `toc_entry_id`.

---

## Part 4 — API & UI

### API (`app/routes/sources.py`)
- `POST /api/sources/pdf` — multipart: `product_id`, `name`, and **either**
  `pdf_url` (creates a `source_type="pdf"` source with `base_url=pdf_url`) **or** a
  `file` upload (creates the source, then writes `<pdf_dir>/<source_id>.pdf` and sets
  `base_url="file://<source_id>.pdf"`). Rejects files over `pdf_max_upload_bytes`
  (413) and non-PDF content types (415).
- `PUT /api/sources/{id}/pdf` — replace the stored file for an upload-origin PDF
  source (multipart `file`). 409 if the source is not an upload-origin PDF.
- `SourceResponse` gains `source_type`.
- Triggering extraction uses the existing `POST /api/extraction/trigger/{source_id}`.

### UI (`frontend/src/components/SourceList.tsx`)
The "add source" form gets a small type selector:
- **Web URL** — today's flow (unchanged).
- **PDF from URL** — name + PDF URL → `POST /api/sources/pdf` with `pdf_url`.
- **PDF upload** — name + file picker → multipart `POST /api/sources/pdf` with
  `file`.
Each source row shows a small `PDF` badge when `source_type === "pdf"`. Re-upload
(for upload-origin PDFs) is offered from the row. `client.ts` gains
`createPdfSourceFromUrl`, `uploadPdfSource`, `replacePdfFile`; `types/index.ts`
gains `source_type` on `DocumentationSource`.

---

## Testing

Backend (sync `Session` + `httpx.AsyncClient` conventions already in `tests/`):
- `segment_pdf`: a fixture PDF **with** an outline yields one segment per outline
  entry with correct page ranges, levels, and paths; a fixture PDF **without** an
  outline falls back (heuristic) to ≥1 segment and never zero.
- `derive_pdf_topic_key`: stable slug from a path; collisions disambiguated.
- `segment_to_markdown`: returns non-empty markdown for a known segment.
- `run_pdf_extraction`: end-to-end on a small fixture PDF creates the expected
  `Article` rows (count, titles, order); a second run with the **same** bytes
  fast-paths to all-unchanged; a run with a **modified** fixture diffs (new/updated)
  via the existing versioning, producing `ArticleVersion` rows.
- `POST /api/sources/pdf`: URL form creates a `pdf` source; upload form stores the
  file and sets the `file://` marker; oversize → 413; non-PDF → 415.
- LLM fallback is exercised with the LLM call stubbed (no network), asserting the
  segmenter consumes the stubbed `{title, level}` list.

Frontend gate: `npm run build` clean; `npm run lint` stays at 0.

Fixtures: generate tiny PDFs in-test with PyMuPDF (one with bookmarks, one without)
so no binary blobs are committed.

---

## Non-goals (v1)

- **OCR / scanned PDFs.** A text layer is required. The LLM fallback segments
  *extracted text*; it is not vision-OCR. Scanned-PDF support is a later iteration.
- **Per-figure image extraction from PDFs.** Inline images are left as PyMuPDF's
  default markdown handling; the dedicated image-download pipeline used for web
  sources is not extended to PDFs in v1.
- No change to web-source extraction behavior beyond the early `source_type` branch.

## Dependencies & licensing

- Add `pymupdf4llm` and `PyMuPDF` (`fitz`) to `backend/requirements.txt`. PyMuPDF is
  **AGPL-3.0** — acceptable for this self-hosted deployment; noted here so it is a
  conscious choice. `pypdf` (already present) is not sufficient for quality
  text→markdown and outline-span extraction.
