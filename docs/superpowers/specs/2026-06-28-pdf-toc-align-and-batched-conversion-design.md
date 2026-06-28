# PDF TOC alignment fix + page-batched docling conversion

**Date:** 2026-06-28
**Status:** Design — approved; pending spec review
**Area:** `backend/app/services/{firecrawl,pdf_convert,docling_client}.py`, `backend/app/core/config.py`

Two follow-ups found after deploying the pipeline-reliability work (PR #97), shipped together.

## Problem 1 — PDF articles misaligned to the TOC

CloudAlly's articles don't match the TOC: the 1st chapter (Preface) is near-empty and "2.1 Prerequisites" shows 2.2's content. The content is segmented correctly, but the **article→TOC-entry linkage is wrong**: child articles point at their *parent's* TOC entry, and ~42 of 119 TOC entries are orphaned.

**Root cause (confirmed):** `FirecrawlService._reconcile_removals` re-links **every** article to a TOC entry by `TOCEntry.url == Article.source_url` (`LIMIT 1`). That assumes one article per URL — true for the web path. PDF article URLs are `…#page=N`, and **multiple sections share a page** (CloudAlly page 6 → 4 sections), so the subquery maps all same-page articles to the *first* TOC entry on that page, clobbering the correct per-segment `toc_entry_id` that `process_article_result` had just set. Verified in the live DB: 119 articles, 119 TOC entries, only 77 distinct `toc_entry_id` linked; same-page articles share one entry.

**Fix:** the URL re-link exists only to repair articles a *resumed* run skipped (their `toc_entry_id` was NULLed by the TOC rebuild and never re-set). `process_article_result` already sets the correct per-segment `toc_entry_id` for every page it processes — both web and PDF. So restrict the re-link to articles whose `toc_entry_id` **is currently NULL**, instead of overwriting all:

```python
update(Article)
  .where(Article.source_id == source_id, Article.toc_entry_id.is_(None))
  .values(toc_entry_id=relink)
```

This is a no-op for the web path (URLs unique, links already set) and stops the PDF clobbering. The subsequent "newly removed" / "re-added" stamping is unchanged (still keyed on `toc_entry_id IS NULL`). PDF runs never resume-skip, so their articles are never NULL at reconcile time and the shared-`#page` URL ambiguity never bites.

After deploy, CloudAlly needs one re-extraction to heal the stored linkage.

## Problem 2 — Largest PDF OOMs docling-serve

The 152-page, image-heavy HYCU User Guides crashes docling-serve (502 on the status poll → restart), so it falls back to pymupdf (lower fidelity) and churns. The 89- and 119-page docs convert fine. Root cause is docling-serve memory on the whole-document conversion (image-heavy). The server is being given more RAM, but the app should also not hand docling a document large enough to OOM it.

**Fix — page-batched conversion.** When a PDF exceeds a page threshold, convert it through docling in `page_range` chunks and stitch, so docling never loads the whole doc at once.

- New setting `pdf_convert_batch_pages: int = 80`.
- `convert_pdf`: open the PDF, read `page_count`. If `page_count <= pdf_convert_batch_pages` → single `convert_async` (today's path). Else → `_convert_docling_batched`.
- `_convert_docling_batched(pdf_bytes, page_count, on_poll)`: for each contiguous range of ≤`pdf_convert_batch_pages` pages (1-based, inclusive), `await convert_async(pdf_bytes, page_range=(start,end), image_export_mode="embedded", page_break_placeholder=_PAGE_BREAK, on_poll=on_poll)`; collect the batch `document` dicts; return `_merge_docling_docs(batch_docs)`.
- `_merge_docling_docs(batch_docs) -> dict`: `md_content` = the batch markdowns joined with `\n{_PAGE_BREAK}\n` (so the stitched markdown is one continuous placeholder-delimited page stream); `json_content` = `{"texts": concat of all batches' texts, "tables": concat of all batches' tables}`. docling reports **absolute** page numbers (verified: `page_range=[7,9]` → `page_no` 7,8,9), so no offset is needed.
- The merged dict is handed to the existing `_build_converted_doc` (under `asyncio.to_thread`), which strips placeholders → `page_line_starts`, content-addresses embedded images, and parses headings/tables — all unchanged. `page_texts` still come from `fitz` over the whole PDF.

Downstream (`split_into_segments`, escalation, TOC build) consumes the stitched `ConvertedDoc` unchanged.

## Module changes

- **`docling_client.py`** — no API change; `convert_async` already accepts `page_range`. (Batching calls it per range.)
- **`pdf_convert.py`** — `convert_pdf` branches single vs batched on `page_count`; add `_convert_docling_batched` and `_merge_docling_docs`. `_build_converted_doc` reused as-is.
- **`firecrawl.py`** — `_reconcile_removals` re-link restricted to NULL `toc_entry_id`.
- **`config.py`** — `pdf_convert_batch_pages: int = 80`.

## Error handling

- Any batch raising `DoclingServeError` (after its own transient-502 retries) → propagate so `convert_pdf` falls back to whole-doc `_convert_pymupdf` (never "no output"). Per-batch fallback is a future refinement, not v1.
- Empty/whitespace stitched markdown → pymupdf fallback (existing guard).
- TOC re-link change cannot orphan a survivor: any article processed this run has a non-NULL `toc_entry_id`; only genuinely-dropped topics stay NULL and are stamped removed.

## Testing

Unit (mock the docling client; synthetic `fitz` PDFs):
- `_reconcile_removals`: two articles sharing a `#page=N` `source_url`, each pre-linked to its own TOC entry by `process_article_result`, are **not** collapsed (links preserved); an article with NULL `toc_entry_id` whose URL matches a TOC entry is re-linked; an article whose URL matches nothing is stamped removed.
- `_merge_docling_docs`: joins markdown with the placeholder and concatenates texts/tables.
- `convert_pdf`: a PDF with `page_count > pdf_convert_batch_pages` calls `convert_async` once per batch with the right `page_range`s and returns a stitched `ConvertedDoc` whose `page_line_starts` covers all pages; `<=` threshold still does a single call; a batch error → pymupdf fallback.
- Existing `convert_pdf`/split/escalate tests stay green.

Validation (live): re-extract CloudAlly → TOC aligned (each TOC entry has its own article; Preface non-empty if it has body; "2.1" shows 2.1). Re-extract HYCU User Guides → converts via **docling** in batches without OOM/502, `attempts=1`.

## Rollout

After deploy, re-extract CloudAlly (heal linkage) and the HYCU User Guides (get docling via batching). No schema change.
