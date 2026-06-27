# Robust PDF → Markdown conversion (Docling + heading-split + VLM escalation)

**Date:** 2026-06-27
**Status:** Design — approved, pending spec review
**Area:** `backend/app/services/pdf_import.py` and new sibling modules

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
   `Nutanix AOS` and `VMware vSphere` both on page 5 (0-based: page 5);
   `XenServer` / `Azure Local` / `Hyper-V` all on page 6. The `Nutanix AOS`
   article comes out containing the full `VMware vSphere` heading and body.

2. **Boundary table truncation + duplicate/mangled rendering.** A table that
   straddles a segment's first/last page is cut; and `pymupdf4llm` sometimes
   emits the same table twice — once as broken loose text (split headers,
   orphaned `l` bullets) and once as a markdown table — and misattributes it to
   the wrong heading because of the bleed above.

`pymupdf4llm`'s heuristic table/layout detection is also the source of the
general "messed formatting" on complex pages.

There are therefore **two independent levers**: (A) *how we split* — page-range
bleed is structural and persists under any engine; (B) *conversion engine
quality* — tables/layout fidelity.

## Goals

- Eliminate cross-section bleed and boundary table truncation.
- Produce clean, well-formed Markdown for tables and complex layouts.
- Keep the existing DB / diff / versioning / TOC machinery unchanged.
- Bound cost: no per-page LLM by default; escalate only hard pages.
- Never regress to "no output": always have a fallback.

## Non-goals

- Changing the web (Firecrawl) extraction path.
- Changing the export/split engine, article schema, or incremental-diff logic.
- Perfect pixel fidelity on every PDF.

## Design

Two layers. **Layer A** (split + default engine) is mandatory and fixes the
structural problems. **Layer B** (VLM escalation) is an automatic,
quality-gated quality lever for the hard remainder.

### Module layout

`pdf_import.py` currently mixes acquire / segment / render / persist (~476 lines).
Split into focused units:

- **`pdf_import.py`** — orchestration only: `run_pdf_extraction`, `acquire_pdf`,
  the byte-hash fast-path, TOC-tree build, and the `process_article_result` /
  `_reconcile_removals` wiring. Unchanged DB behaviour.
- **`pdf_convert.py`** *(new)* — Docling conversion + heading-split. Public
  surface:
  - `convert_pdf(pdf_bytes) -> ConvertedDoc` — whole-doc conversion (markdown +
    structured items with page provenance), runs the heavy work synchronously
    (caller offloads via `asyncio.to_thread`).
  - `split_into_segments(converted, outline) -> list[Segment]` — split at
    heading boundaries (see below). `Segment` keeps its current shape plus the
    rendered markdown and image list.
- **`pdf_escalate.py`** *(new)* — `score_segment(...) -> list[Issue]` and
  `escalate_segment(pdf_bytes, segment, ...) -> str` (renders pages to images,
  calls the VLM, returns replacement markdown).
- **`profiles/llm.py`** — extend `call_llm` with an optional
  `images: list[bytes] | None` parameter (see "VLM transport").

### Layer A — convert once, split on headings

1. **Convert the whole PDF once** with Docling, off the event loop
   (`asyncio.to_thread`, preserving the worker heartbeat — the concern PR #90
   addressed). Docling yields a `DoclingDocument`: full-document markdown in
   reading order, a heading hierarchy, table structures, and **page provenance**
   per item. Converting the whole document at once keeps cross-page tables whole
   and lets Docling strip running headers/footers globally.

2. **Split into article segments at heading boundaries, never page ranges:**
   - **Outline present:** keep the PDF bookmark outline as the article/TOC
     structure (preserves `topic_key` derivation and therefore incremental-diff
     stability). For each outline entry, find its split point by locating the
     entry's heading text in the Docling output (using Docling's heading items;
     page provenance disambiguates duplicate titles and is the fallback when a
     title is not found verbatim). Each segment's markdown is the slice between
     its heading and the next entry's heading.
   - **No outline:** derive segments directly from Docling's detected heading
     hierarchy. **This replaces the current `heuristic_segments` (font-size) and
     `_llm_segment_titles` (LLM) fallbacks**, which are removed — Docling's
     layout model identifies headings far more reliably.

3. **Images.** Preserve current behaviour: extract figures, content-address them
   (`<sha16>.png`), rewrite markers, dedupe. Docling can export images; the
   existing content-addressing/rewrite logic in `_render_segment` is adapted to
   operate on the whole-doc output and slice per segment.

4. **Engine fallback.** If Docling raises or yields empty output for a document,
   fall back to a whole-doc `pymupdf4llm.to_markdown(doc)` conversion (no page
   ranges) and split that on its markdown headings. Guarantees we never produce
   no output and that even the fallback avoids page-range bleed.

### Layer B — quality-gated VLM escalation

After Layer A produces segments, score each and escalate only the weak ones.

**Confidence signals (per segment):**
- **Ragged table:** a markdown table whose body rows have inconsistent column
  counts vs. the header (beyond a small tolerance), or a header with zero/near-
  zero body rows while Docling reported a multi-row table region on those pages.
- **Missing table:** Docling detected a table region on the segment's pages but
  the segment markdown contains no table.
- **Sparse text:** segment markdown character count is far below the raw
  `fitz` `get_text` length for the same pages (extraction dropped content).

**Escalation:** for a flagged segment, render its page(s) to PNG via `fitz` at
`pdf_vlm_dpi`, send to the VLM with a fixed instruction ("Convert this page to
clean GitHub-Flavored Markdown; preserve tables as Markdown tables; output only
the Markdown, no commentary"), and replace the segment body with the result.
Bounded by `pdf_vlm_max_pages_per_run` — once the run's escalation page budget
is spent, remaining flagged segments keep their Docling output.

**Granularity:** v1 escalates at **segment** granularity (render the segment's
pages, replace its whole body). Sub-segment/region replacement is a future
refinement. Docling tables are generally good, so flags — and thus token
spend — should be infrequent.

### VLM transport (OpenRouter, not Claude)

The VLM uses **OpenRouter** (OpenAI-compatible) with its own configuration,
independent from the segmentation `llm_*` settings (which may remain Anthropic).

- `call_llm(prompt, *, system=None, images=None)` gains `images`:
  - **openai/OpenRouter branch:** user content becomes a list —
    `[{"type":"text","text":prompt}, {"type":"image_url","image_url":
    {"url":"data:image/png;base64,<…>"}}, …]`.
  - **anthropic branch:** emit the block shape
    (`{"type":"image","source":{"type":"base64",...}}`) for completeness, though
    the default VLM path is OpenRouter.
  - Text-only callers (PDF segmentation, profile derivation) are unaffected.
- Escalation calls `call_llm` against the dedicated VLM settings (a thin helper
  in `pdf_escalate.py` builds an OpenRouter request from `pdf_vlm_*` rather than
  the global `llm_*`).

### New settings (`DOCEXTRACTOR_` prefix)

| Setting | Default | Purpose |
|---|---|---|
| `pdf_converter` | `docling` | `docling` \| `pymupdf` — default engine for Layer A |
| `pdf_vlm_escalation_enabled` | `true` | Master switch for Layer B |
| `pdf_vlm_base_url` | `https://openrouter.ai/api/v1/chat/completions` | VLM endpoint |
| `pdf_vlm_api_key` | `""` | OpenRouter key (empty → escalation skipped) |
| `pdf_vlm_model` | `qwen/qwen3-vl-32b-instruct` | Cheap, strong-on-docs vision model |
| `pdf_vlm_max_pages_per_run` | `30` | Token guardrail |
| `pdf_vlm_dpi` | `150` | Render resolution for escalated pages |

### Deployment

- Add `docling` to `requirements.txt` (pinned).
- **Pre-bake Docling models into the worker image** at Docker build time so the
  first run does not stall downloading models (the deploy bakes code/deps into
  images — `[[deploy-workflow]]`, `[[k8s-deployment]]`).
- Conversion remains off the event loop via `asyncio.to_thread`, preserving the
  heartbeat (`[[worker-event-loop-heartbeat]]`). Progress reporting becomes
  coarser per phase (`pdf_convert` → `pdf_split` → `pdf_escalate`); log phase
  transitions and escalation counts.

## Error handling

- Docling failure/empty → `pymupdf4llm` whole-doc fallback (still heading-split).
- VLM disabled, no API key, or HTTP error → skip escalation silently; keep
  Docling output (warning logged).
- Escalation page budget exhausted → remaining flagged segments keep Docling
  output.
- A segment rendering to empty markdown is not persisted (existing behaviour) and
  excluded from `articles_total`.

## Testing

Unit (deterministic; LLM/Docling mocked, synthetic `fitz`-built PDFs in the
existing test style):
- Heading-split produces **no cross-section bleed**: a two-section-on-one-page
  fixture yields disjoint article bodies.
- Split **never cuts a table** that crosses a page break.
- No-outline path derives segments from Docling headings.
- Confidence scorer flags a ragged table and a missing table; passes a clean one.
- `call_llm(images=…)` builds the correct OpenAI/OpenRouter and Anthropic
  payload shapes (mocked transport, no network).
- Docling-failure → `pymupdf` fallback path.

Validation (manual, real PDF): run the new pipeline on
`HYCU_CompatibilityMatrix.pdf` and confirm: `Nutanix AOS` no longer contains
`VMware vSphere`; tables render once, intact, under the correct heading;
multi-page tables are whole.

## Rollout / reversibility

- `pdf_converter=pymupdf` reverts Layer A's engine; `pdf_vlm_escalation_enabled=
  false` disables Layer B — both via env, no code change.
- No schema migration: articles, TOC, runs, and the diff path are unchanged.

## Open risks (resolve during planning)

- Docling install size / CPU conversion latency on the worker — validate the
  pinned version installs and converts the HYCU PDF in acceptable time.
- Mapping outline titles → Docling output offsets when a title appears verbatim
  in body text (mitigated by page provenance + order).
