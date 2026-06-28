# PDF pipeline reliability: heartbeat, progress monitoring, and segment-drop fix

**Date:** 2026-06-28
**Status:** Design — approved direction; pending spec review
**Area:** `backend/app/services/{pdf_import,pdf_convert,docling_client}.py`, `frontend/src/components/SourceList.tsx`

Builds on the docling-serve conversion work (PR #95). Three defects/improvements found while re-extracting all PDF sources, all in the PDF pipeline, shipped together.

## Problem 1 — Event-loop starvation reaps long conversions (churn)

Re-extracting the largest PDF (HYCU User Guides, 285 articles / 152 pages) showed a second PDF starting before the first finished, plus repeated restarts.

**Root cause (confirmed):** `convert_pdf` runs heavy work **synchronously on the asyncio event loop** with no thread offload:
- `docling_client.convert` → `resp.json()` parses the docling response. With `image_export_mode=embedded` a large doc's response is huge (every figure base64-inlined), so the parse blocks for a long time.
- `pdf_convert._content_address_data_uris` (regex over all markdown + base64-decode every image), `_parse_*`, and `_page_texts` (`fitz.get_text` over every page) — all synchronous.
- the `_convert_pymupdf` fallback — synchronous.

The worker's `_heartbeat` task (15 s interval) and `_flush_logs` task can't run while the loop is blocked. For the largest PDF the block exceeds the reaper's `stale_seconds=300`, so `reap_stale_runs` resets the run RUNNING→PENDING (`attempts++`). The single, sequential worker then claims the next PENDING run — the "second PDF before the first finished" — and the big PDF re-runs until it FAILS at `attempts=3`. This is the same class as PR #90 (`worker-event-loop-heartbeat`), reintroduced by the whole-document pipeline. The network wait itself is fine (it's awaited; the loop stays free and the heartbeat ticks — verified live at age 290 s with a 19 s heartbeat).

**Fix:** run the synchronous CPU work off the event loop via `asyncio.to_thread`, so the heartbeat/log-flush tasks keep ticking. This covers `resp.json()`, the docling response→`ConvertedDoc` transform, and the pymupdf fallback.

## Problem 2 — No usable progress/monitoring during whole-document conversion

The convert phase is a single docling call (the service exposes only document-level async status — `num_docs`/`num_processed` — never page-level), so the UI shows a frozen `0/0` bar and the run log is nearly empty. Escalation also misused `articles_extracted` (overwrote it with a flagged-segment count).

**Fix (agreed appetite: phases + denominator + logs + async polling heartbeat):**
- Switch the main conversion to docling-serve's **async** endpoints (`POST /v1/convert/source/async` → poll `GET /v1/status/poll/{task_id}` → `GET /v1/result/{task_id}`), invoking an `on_poll` callback each tick. New setting `docling_serve_poll_interval` (default 3.0 s); overall deadline = existing `docling_serve_timeout`.
- `current_phase` progression: `pdf_acquire → pdf_convert → pdf_split → pdf_escalate → content_scraping`. The last is the existing persist loop where `articles_extracted` genuinely advances.
- Set `articles_total = len(outline)` **before** convert so the bar has a denominator immediately.
- Stop overwriting `articles_extracted` during escalation (log per-segment instead).
- Emit `logger.info` lines (auto-captured to `run.log_text` via `_RunLogHandler`): pages sent; per-poll "still converting — Ns elapsed (status/queue pos)"; "converted in Xs — M sections, K tables"; "N segments"; "J flagged; re-converting via VLM"; per-segment escalation.
- Frontend `SourceList.tsx`: map the new PDF phases to the existing indeterminate-bar + friendly-label pattern (as `toc_discovery` already does): `pdf_acquire`→"Downloading PDF…", `pdf_convert`→"Converting document…", `pdf_split`→"Splitting into articles…", `pdf_escalate`→"Refining low-confidence sections…". `content_scraping` keeps the determinate bar.

The single long await already keeps the heartbeat alive; async polling adds liveness logging and avoids one multi-minute HTTP request risking proxy/idle timeouts. (The `to_thread` offload from Problem 1 remains required: `GET /v1/result` still returns a large JSON to parse.)

## Problem 3 — Outline entries silently dropped → articles wrongly removed

Re-extracting CloudAlly (MS 365 User Guide, 119 outline entries) produced only **14** segments; `_reconcile_removals` then marked ~94 articles removed.

**Root cause (reproduced):** `split_into_segments` locates each outline title as a heading line in docling's markdown via `_find_heading_line` (whitespace-normalized substring match) and **`continue`s (drops the entry) on a miss**. The texts diverge:
- numbering spacing: outline `1Preface` / `1.1About this Guide` vs docling `1 Preface` / `1.1 About this Guide`;
- HTML entities (`&amp;`) and unicode punctuation (curly `’` vs straight `'`).
Only 20/119 matched → 14 segments. The spec's intended page-provenance fallback was never implemented.

**Fix:**
1. **Robust matching** in `_find_heading_line`: compare on a normalized core — unescape HTML entities, normalize unicode punctuation, casefold, and strip all non-alphanumerics (so `1Preface`↔`1 Preface` → `1preface`; `What’s`↔`What's` → `whats`). Verified offline against the live docling headings: this matches 119/119.
2. **Never drop** an outline entry (defense in depth): request docling with `md_page_break_placeholder` (a sentinel inserted between pages), build a page→markdown-offset map, and for any still-unmatched title place its boundary at the start of its `page_start` page. Guarantees every outline entry yields a segment, partitioned by page when title matching fails — no silent content loss.

## Module changes

- **`docling_client.py`** — add `convert_async(..., on_poll=None) -> dict` (submit/poll/result); parse the result JSON via `asyncio.to_thread`. Sync `convert` stays for VLM escalation (bounded per segment). Request `md_page_break_placeholder` on the standard conversion.
- **`pdf_convert.py`** — `convert_pdf(pdf_bytes, on_poll=None)` uses `convert_async`; the response→`ConvertedDoc` transform (`_content_address_data_uris`, `_parse_*`, `_page_texts`) and `_convert_pymupdf` run under `asyncio.to_thread`. `ConvertedDoc` gains a page-offset map (from page-break placeholders). `_find_heading_line` normalization upgrade. `split_into_segments` page-provenance fallback (never drop).
- **`pdf_import.py`** — `build_segments(pdf_bytes, outline, on_poll=None)`; `run_pdf_extraction` sets the denominator early, drives phases, emits logs, fixes escalation progress.
- **`frontend/src/components/SourceList.tsx`** — phase→label/indeterminate mapping.
- **`config.py`** — `docling_serve_poll_interval: float = 3.0`.

## Error handling

- Async submit/poll/result failure or timeout → `DoclingServeError` → `_convert_pymupdf` fallback (unchanged guarantee: never "no output").
- `on_poll` callback wrapped so it can never crash the run.
- Page-provenance fallback only triggers for unmatched titles; matched titles keep precise heading-boundary slicing.

## Testing

Unit (mock the docling HTTP client + httpx; synthetic `fitz` PDFs):
- `convert_async`: submit→poll(started→success)→result; `on_poll` fired each tick; failure status & timeout raise.
- `convert_pdf`: offloads via `to_thread` (assert it awaits/returns correct `ConvertedDoc` from a mocked client); fallback on error.
- `_find_heading_line` normalization: `1Preface`↔`1 Preface`, entity/quote cases match; non-matches still don't false-match.
- `split_into_segments`: an outline entry whose title is absent from the markdown still yields a segment via the page-break fallback (no drop); a two-section-shared-page case still has no bleed.
- `build_segments`/phases: denominator set from outline before convert; escalation no longer overwrites `articles_extracted`.
- Frontend: `npm run build`.

Validation (live): re-extract CloudAlly → 119 articles restored (0 wrongly removed); re-extract HYCU User Guides → completes without reaping/restart, log shows phase/heartbeat lines; spot-check the UI shows "Converting document…" then a moving persist bar.

## Rollout

After deploy, re-extract the PDF sources that were churned/mis-reconciled (CloudAlly + HYCU User Guides at minimum). No schema change.

## Validation result (2026-06-28)

- **Backend suite:** 771 passed (was 750; +21 new tests).
- **Problem 3 (CloudAlly segment-drop) — FIXED:** end-to-end against live docling-serve, the MS 365 User Guide now yields **119 segments (all non-empty)** vs. the broken **14**. `outline=119, segments=119`.
- **Extra robustness fix (found during validation):** docling-serve returned a transient **502 on a status-poll** during the long CloudAlly convert, which abandoned the conversion (→ pymupdf fallback). Added transient-error tolerance to `convert_async` (retry poll/result GETs within the deadline). After the fix the same convert completes via **engine=docling** (not the fallback).
- **Problems 1 & 2 (heartbeat/monitoring):** the off-loop `to_thread` offload, async polling, phase labels, early denominator, and run-log lines are in place and unit-tested. Live confirmation (no reaping/restart on the 285-article User Guides; UI phase progression) to be observed after deploy + re-extraction.
