# PDF Export — Design

**Date:** 2026-06-17
**Status:** Approved (design); pending implementation plan
**Goal addressed:** "export it in different formats (e.g., markdown, pdf)" (CLAUDE.md) — adds the missing PDF format to the export engine.

## Summary

Add PDF as an export format alongside markdown. All selection and splitting behaviour is **reused unchanged**; only the per-group render and packaging branch on the format. A PDF export honours the same `split_by` / `respect_chapters` grouping as markdown (one PDF per article-group, never splitting a single article), embeds images inline so each PDF is self-contained, and is delivered through the existing ZIP + download route.

## Decisions (locked during brainstorming)

| Decision | Choice | Rationale |
|---|---|---|
| Splitting | PDF honours the same `split_by`/`respect_chapters` grouping as markdown | Consistent UI/behaviour for both formats; reuses the existing "never split an article" grouping. |
| Size-split metric | Still measured on markdown bytes (a proxy) | Grouping is computed before rendering; PDF byte size differs but the article grouping is identical. Acceptable; documented. |
| Packaging | Always a ZIP via the existing `GET /api/export/download/{id}` | One download path for both formats; no route changes. (A future tweak could serve a lone PDF directly.) |
| Images | Embedded inline in each PDF (no `images/` dir for PDF bundles) | Self-contained PDFs; simpler than the markdown path's copy+rewrite. |
| Library | WeasyPrint (HTML→PDF) + Python `markdown` | Best-quality CSS/layout and inline image embedding. |
| Where it runs | In the export request path (same as markdown today) | Fine for typical sizes; can move to the worker queue later if needed. |

## Request & selection (shared, unchanged)

`ExportRequest` gains:

```python
format: str = "markdown"   # "markdown" | "pdf"
```

Everything up to rendering is unchanged: `article_ids` / `toc_entry_ids` / `topic_query` selection, `split_by` (`size`|`articles`|`tokens`), the per-article-metric limits, `respect_chapters`, and the grouping guarantee that a single article is never split across files. The format only changes how each resolved group is rendered and bundled.

## Rendering pipeline

New module **`app/services/pdf_renderer.py`**, one public function:

```python
def render_markdown_to_pdf(markdown_text: str, base_url: str) -> bytes
```

It converts markdown → HTML (Python `markdown` with the `tables`, `fenced_code`, and `toc` extensions), wraps it in a small print stylesheet (readable body font, page margins, sensible heading/code/table styling, clickable in-document TOC anchors), and renders to PDF bytes via WeasyPrint with the given `base_url`. Keeping WeasyPrint behind this single function isolates the heavy dependency and makes it unit-testable.

The exporter builds the **same** `_build_markdown_document(group, source_name)` output it uses for markdown (so PDF and markdown content are identical), then for `format == "pdf"` calls `render_markdown_to_pdf` per group instead of writing a `.md` file.

## Images

The markdown path rewrites `/media/<id>/<file>` → relative `images/<id>/<file>` and copies the files into the bundle. The PDF path instead rewrites the served `/media/` prefix so the paths resolve against `base_url = <media_root>` (the canonical image dir on disk), and WeasyPrint embeds the referenced images directly into the PDF. Consequence: PDF bundles contain only the `.pdf` file(s) — no `images/` directory. Missing image files are skipped (the renderer must not fail the whole export if one image is absent), mirroring the markdown path's tolerance.

## Output & packaging

`_generate_export` branches per group on `format`:
- **markdown** (unchanged): write `<name>.md` / `<name>_partNNN.md`, rewrite to relative `images/`, copy images, zip with the images dir.
- **pdf**: render each group to `<name>.pdf` / `<name>_partNNN.pdf`, write the bytes, add to the archive; no image copying. `ExportFileInfo.filename` carries the `.pdf` name; `size_bytes` is the PDF's byte size.

The single self-contained ZIP and the `GET /api/export/download/{export_id}` route are unchanged.

## Dependencies

- `requirements.txt`: add `weasyprint` and `markdown`.
- Backend `Dockerfile`: `apt-get install` WeasyPrint's native libraries (pango, cairo, gdk-pixbuf, and base fonts) before `pip install`. This enlarges the image and requires a rebuild — the one real cost of this feature.

## Frontend

`ExportPanel` gains a **Format** toggle (Markdown / PDF), defaulting to Markdown. The split-by and respect-chapters controls remain enabled for both formats. The request sends `format`; the response and the ZIP download flow are unchanged. `ExportRequest` TS type gains `format?: "markdown" | "pdf"`.

## Testing

- **`pdf_renderer`** (unit): `render_markdown_to_pdf` returns bytes starting with `%PDF`, non-trivial length; rendering markdown that references a real on-disk image embeds it (PDF size grows vs. the same doc without the image) and a missing image path does not raise.
- **Exporter**: `format="pdf"` produces `.pdf` members (one per group when `split_by` is set), no `images/` dir; `format="markdown"` behaviour is unchanged (regression).
- **Route**: `POST /api/export/markdown` (or the export route) accepts `format="pdf"` and returns an `ExportResponse` whose files are `.pdf`; download serves the ZIP.
- **Frontend**: `npm run build` + `npm run lint` clean.

## Out of scope
- Serving a lone PDF un-zipped (always ZIP for now).
- Custom PDF theming/branding, cover pages, headers/footers with page numbers beyond basic styling.
- Moving PDF rendering to the worker queue (stays in-request for now).
- Per-PDF size-limit accuracy (the `size` split stays a markdown-byte proxy).
