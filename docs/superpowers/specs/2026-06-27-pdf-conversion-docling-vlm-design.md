# Robust PDF → Markdown conversion (docling-serve + heading-split + VLM escalation)

**Date:** 2026-06-27
**Status:** Implemented & validated against live docling-serve (2026-06-27)
**Area:** `backend/app/services/pdf_import.py` and new sibling modules

## Validation result (2026-06-27, live `http://docling.home.lan` v1.12.0)

Ran the implemented pipeline on `HYCU_CompatibilityMatrix.pdf` (24 pages).

**Layer A (standard convert + heading-split) — PASS.** 23 segments (one per
outline entry). The `Nutanix AOS` article no longer contains `VMware vSphere`
(cross-section **bleed eliminated**), and its compatibility table renders **once,
intact, correctly attributed** — versus the original mangled + duplicated +
misattributed output. Conversion ~16 s.

**Layer B (VLM escalation via OpenRouter qwen) — PASS (round-trip), with a
design refinement.** The OpenRouter `qwen/qwen3-vl-32b-instruct` path through
docling-serve's `pipeline=vlm` works end-to-end (HTTP 200, markdown returned).
Two findings, both addressed:

1. **`response_format` is required** in `vlm_pipeline_model_api` — added
   (`"markdown"`). Without it docling-serve returns 422 and escalation silently
   falls back.
2. **Page-level escalation re-bleeds on shared pages.** docling's VLM
   re-conversion of a page returns *everything* on that page; a segment that
   shares its page with siblings/parent would pull their content back in. Fixed:
   escalation now only fires for segments that **exclusively own their page
   range** (`build_segments`). On HYCU all 6 flagged segments were shared-page
   parent/intro sections, so none escalate — correct, since docling's standard
   output for them is already clean. (Observed bonus: on the AOS page, qwen's
   table was actually *worse* than docling's standard output — confirming VLM
   should stay a reserved safety net, not the default.)

## Problem

PDF sources extract unreliably. Articles show leading/trailing disconnected
text, truncated tables, and messed formatting. The HYCU User Guide (and the
smaller HYCU Compatibility Matrix used for validation) are representative.

### Root cause (reproduced)

The current path (`pdf_import.py`) segments a PDF by **page range** derived from
the bookmark outline, then converts each page range **independently** with
`pymupdf4llm.to_markdown(doc, pages=range)`. Two structural flaws follow:

1. **Page-range bleed.** When several outline sections start on the *same* page
   (very common), every one of those segments is assigned that whole page and
   renders all of it — so each article contains its neighbours' content.
   Reproduced on `HYCU_CompatibilityMatrix.pdf`: the outline places
   `Nutanix AOS` and `VMware vSphere` both on page 6; `XenServer` / `Azure Local`
   / `Hyper-V` all on page 7. The `Nutanix AOS` article comes out containing the
   full `VMware vSphere` heading and body.

2. **Boundary table truncation + duplicate/mangled rendering.** A table that
   straddles a segment's first/last page is cut; `pymupdf4llm` sometimes emits
   the same table twice (once as broken loose text, once as a table) and
   misattributes it to the wrong heading because of the bleed above.

`pymupdf4llm`'s heuristic table/layout detection is also the source of the
general "messed formatting" on complex pages.

Two independent levers: (A) *how we split* — page-range bleed is structural and
persists under any engine; (B) *conversion engine quality* — tables/layout.

## Goals

- Eliminate cross-section bleed and boundary table truncation.
- Produce clean, well-formed Markdown for tables and complex layouts.
- Keep the existing DB / diff / versioning / TOC machinery unchanged.
- Bound cost: no per-page LLM by default; escalate only hard pages.
- Never regress to "no output": always have a fallback.
- Keep the heavy ML stack out of the app image.

## Non-goals

- Changing the web (Firecrawl) extraction path.
- Changing the export/split engine, article schema, or incremental-diff logic.
- Deploying docling-serve (already running on the homelab; see Infra).

## Infrastructure (decided)

Docling runs as a **docling-serve** instance on the homelab k3s, reachable at
`http://docling.home.lan` (version 1.12.0), authenticated with an **`X-Api-Key`**
header. The app consumes its REST API exactly like it consumes Firecrawl — no
docling/torch dependency is embedded in the backend image. Validated live: a
24-page PDF converts in ~16 s via `POST /v1/convert/file` returning both
`md_content` and a structured `json_content` (`DoclingDocument`).

docling-serve also exposes a **VLM pipeline** (`pipeline: "vlm"`) that can drive
an **OpenAI-compatible remote model** (`vlm_pipeline_model_api`: `{url, headers,
params}`). The VLM escalation therefore runs *through docling-serve*, pointed at
**OpenRouter** (model `qwen/qwen3-vl-32b-instruct`) — the app forwards the
OpenRouter endpoint/key/model in the request rather than rendering pages and
calling the model itself.

## docling-serve API contract (v1.12.0, confirmed live)

- `POST /v1/convert/file` — multipart. Auth header `X-Api-Key: <key>`.
- Form fields used:
  - `files=@doc.pdf;type=application/pdf`
  - `to_formats=md`, `to_formats=json`
  - `do_ocr=false` (native PDFs; configurable)
  - `image_export_mode=embedded` (base64 data URIs in markdown) — see Images
  - `table_mode=accurate` (default)
  - For escalation: `pipeline=vlm`, `page_range=<start>` `page_range=<end>`
    (1-based, inclusive), and `vlm_pipeline_model_api` as a JSON value:
    `{"url": <openrouter chat completions url>, "headers": {"Authorization":
    "Bearer <key>"}, "params": {"model": "qwen/qwen3-vl-32b-instruct"},
    "prompt": "Convert this page to markdown. Do not miss any text and only
    output the bare markdown!"}`
- Response `ConvertDocumentResponse`:
  - `status` ("success" | …), `processing_time`, `errors[]`
  - `document.md_content` — markdown string
  - `document.json_content` — `DoclingDocument`: `texts[]` (each has `label`
    — e.g. `section_header`, `text`, `page_header`, `page_footer`, `list_item`;
    `text`; `level` for headings; `prov[].page_no` 1-based), and `tables[]`
    (each `prov[].page_no`).

A saved copy of the OpenAPI spec lives at the session scratchpad
(`docling_serve_v1.12_openapi.json`) for reference during implementation.

## Design

Two layers. **Layer A** (convert + split) fixes the structural problems.
**Layer B** (VLM escalation) is an automatic, quality-gated quality lever for
the hard remainder. Both call docling-serve.

### Module layout

`pdf_import.py` currently mixes acquire / segment / render / persist. Split into
focused units:

- **`pdf_import.py`** — orchestration only: `run_pdf_extraction`, `acquire_pdf`,
  byte-hash fast-path, TOC-tree build, `process_article_result` /
  `_reconcile_removals` wiring. Unchanged DB behaviour.
- **`docling_client.py`** *(new)* — thin async HTTP client for docling-serve.
  `convert(pdf_bytes, *, pipeline="standard", page_range=None, vlm_model_api=None)
  -> dict` returns the parsed `document` (md + json). Adds `X-Api-Key`. Raises
  `DoclingServeError` on non-200 / error status.
- **`pdf_convert.py`** *(new)* — `convert_pdf(pdf_bytes) -> ConvertedDoc` (calls
  the client for a standard conversion, parses `json_content` into headings /
  table-pages / images; pymupdf4llm fallback on `DoclingServeError`), and
  `split_into_segments(converted, outline) -> list[RenderedSegment]` (heading-
  boundary split).
- **`pdf_escalate.py`** *(new)* — `score_segment(segment, converted) -> list[str]`
  and `escalate_segment(pdf_bytes, segment) -> str` (re-convert the segment's
  page range via the client's VLM pipeline; return replacement markdown).

### Data flow

1. `acquire_pdf` → bytes (unchanged; byte-hash fast-path unchanged).
2. **Convert once** via docling-serve standard pipeline (`md` + `json`). This is
   network I/O (async), so it does not block the worker event loop — no
   thread-offload needed. Markdown is whole-document (reading order + tables
   whole across page breaks); `json_content` gives heading/table provenance.
3. **Segment by headings, not pages:**
   - *Outline present* → keep the PDF bookmark outline as the TOC/article
     structure (preserves `topic_key` stability), but find each entry's split
     point by locating its heading text in the docling markdown (page provenance
     as fallback). No page-range bleed.
   - *No outline* → derive segments from docling's `section_header` items
     (`label`, `level`, `prov.page_no`). Replaces the old font/LLM fallbacks.
4. **Confidence scoring** per segment: ragged markdown tables (mismatched column
   counts), `json_content` table on the segment's pages but no markdown table,
   and segments whose text is sparse vs. raw `fitz` text for the same pages.
5. **VLM escalation** for flagged segments within a per-run page budget:
   re-`POST /v1/convert/file` with `pipeline=vlm`, `page_range=[start,end]`, and
   the OpenRouter `vlm_pipeline_model_api`. Replace the segment body with the
   returned markdown.
6. Persist via the existing `process_article_result` / `_reconcile_removals`
   path — **unchanged**.

### Images

Request `image_export_mode=embedded`: docling returns figures as base64
`data:image/...;base64,…` URIs inside the markdown. `pdf_convert` rewrites each
data URI to a content-addressed `![alt](<sha16>.png)` reference and collects the
decoded bytes as `RenderedImage`, so the existing image-persistence path
(`pdf_images=` on `process_article_result`) is reused unchanged. (Placeholder
mode is used during scoring-only tests where image bytes are irrelevant.)

### New settings (`DOCEXTRACTOR_` prefix)

| Setting | Default | Purpose |
|---|---|---|
| `pdf_converter` | `docling` | `docling` (remote docling-serve) \| `pymupdf` (in-proc fallback engine) |
| `docling_serve_url` | `http://docling.home.lan` | docling-serve base URL |
| `docling_serve_api_key` | `""` | `X-Api-Key` value (env only — never committed) |
| `docling_serve_timeout` | `600` | per-request read timeout (s) |
| `pdf_vlm_escalation_enabled` | `true` | Master switch for Layer B |
| `pdf_vlm_base_url` | `https://openrouter.ai/api/v1/chat/completions` | forwarded as `vlm_pipeline_model_api.url` |
| `pdf_vlm_api_key` | `""` | forwarded as `Authorization: Bearer` (env only) |
| `pdf_vlm_model` | `qwen/qwen3-vl-32b-instruct` | forwarded as `params.model` |
| `pdf_vlm_max_pages_per_run` | `30` | token/time guardrail |

(The earlier `pdf_vlm_dpi` is dropped — docling-serve handles page rendering; its
`vlm_pipeline_model_api.scale` default is sufficient.)

### Secrets

`docling_serve_api_key` and `pdf_vlm_api_key` are provided via environment
(`DOCEXTRACTOR_…`). **`backend/.env` is git-tracked**, so keys must NOT be
written there; they are supplied via the deployment's secret env and, for local
validation, exported inline on the command.

## Error handling

- docling-serve non-200 / error status / network failure on the standard
  conversion → `pymupdf4llm` whole-doc fallback (still heading-split). Never
  "no output".
- VLM disabled, no OpenRouter key, or docling-serve VLM error → skip escalation;
  keep the standard docling output (warning logged).
- Escalation page budget exhausted → remaining flagged segments keep their
  standard output.
- A segment rendering to empty markdown is not persisted (existing behaviour) and
  excluded from `articles_total`.

## Testing

Unit (deterministic; the docling-serve HTTP client mocked, synthetic `fitz`
PDFs in the existing test style):
- Heading-split: a two-section-on-one-page fixture yields disjoint bodies (no
  bleed); a table crossing a page break stays whole in one slice.
- No-outline path derives segments from docling `section_header` items.
- `convert_pdf` parses a mocked docling-serve response into `ConvertedDoc`
  (headings, table_pages, embedded-image → content-addressed); a client error
  triggers the pymupdf fallback.
- Confidence scorer flags ragged/missing-table/sparse; passes a clean one.
- `escalate_segment` builds the correct `pipeline=vlm` + `page_range` +
  `vlm_pipeline_model_api` request (mocked client) and substitutes the body;
  falls back to original markdown on client error.
- `docling_client` sets `X-Api-Key` and posts the expected multipart fields
  (mocked transport, no network).

Validation (manual, real service + PDF): run the pipeline on
`HYCU_CompatibilityMatrix.pdf` against `http://docling.home.lan` and confirm
`Nutanix AOS` no longer contains `VMware vSphere`, tables render once/intact, and
one VLM escalation round-trips through OpenRouter.

## Rollout / reversibility

- `pdf_converter=pymupdf` reverts Layer A to the in-proc engine;
  `pdf_vlm_escalation_enabled=false` disables Layer B — both via env.
- No schema migration: articles, TOC, runs, diff path unchanged.

## Open risks (resolve during planning)

- Large PDFs may exceed the sync endpoint's practical time; if so, switch to the
  async endpoints (`/v1/convert/file/async` + `/v1/status/poll` + `/v1/result`).
  v1 uses the sync endpoint with `docling_serve_timeout`.
- Mapping outline titles → docling markdown offsets when a title appears verbatim
  in body text (mitigated by page provenance + order).
- VLM escalation latency/cost per flagged segment (bounded by the page budget).
