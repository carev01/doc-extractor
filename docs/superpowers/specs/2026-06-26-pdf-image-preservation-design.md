# Design: PDF image preservation + media GC

Date: 2026-06-26

Two pieces:

1. **Preserve PDF images** — extract images embedded in a PDF during conversion,
   store them on the media volume, serve them over HTTP, and reference them from
   the article markdown. Mirrors the existing web-source image pipeline and its
   hash-stability invariant, with content-hash filenames for incremental safety.
2. **Media GC** — a global sweep that removes orphaned `media_dir/<article_id>/`
   directories whose article no longer exists, for both PDF and web images
   (closes the on-disk leak that hard deletes leave behind today).

---

## Background: the web image pipeline (what we mirror)

`process_article_result` (in `app/services/firecrawl.py`) already preserves web
article images:

- It computes `content_hash` early, on the **canonical** markdown (original remote
  image URLs), then makes the new/updated/unchanged decision on that hash.
- On the new/updated path, after the `Article` row exists, the `if doc_html:`
  block downloads each `<img>` into `media_dir/<article.id>/<filename>`, creates
  an `ArticleImage` row per image, and rewrites the markdown image references from
  the original URL to the served URL `<media_url_prefix>/<article.id>/<filename>`.
- The stored `article.content_markdown` carries the **served** URLs; the hash was
  taken on the **canonical** form — so the per-article-id served paths never
  affect diffs.

`media_dir` is mounted as a `StaticFiles` directory at `media_url_prefix`
(default `/media`) in `app/main.py`. `ArticleImage` rows cascade-delete with the
article. Exports already rewrite `/media` paths to relative ones.

PDF images reuse this exact invariant.

---

## Part 1 — PDF image extraction

### Rendering (in `app/services/pdf_import.py`)

`pymupdf4llm.to_markdown(..., write_images=False, embed_images=False)` (today's
call) **discards** images entirely. Switch the renderer to write images and then
canonicalize them:

**New value object**

```python
@dataclass
class RenderedImage:
    filename: str   # "<sha16>.png" — content-addressed
    data: bytes
    alt: str
```

**`_render_segment(doc, segment) -> tuple[str, list[RenderedImage]]`**
- Inside its own `tempfile.TemporaryDirectory()` (per-segment isolation), call
  `pymupdf4llm.to_markdown(doc, pages=range(...), write_images=True,
  image_path=<tmp>, image_format="png")`. The library writes `-<page>-<index>.png`
  files into the temp dir and emits `![](<tmp>/-p-i.png)` markers in the markdown.
- For each emitted image marker, read the file it points at, compute
  `sha = hashlib.sha256(bytes).hexdigest()[:16]`, set the canonical filename
  `<sha>.png`, and **rewrite that marker's target** in the markdown to the bare
  canonical reference `<sha>.png` (yielding `![<alt>](<sha>.png)`). Collect one
  `RenderedImage(<sha>.png, bytes, alt)` per distinct sha (dedupe identical images
  within the segment). The temp dir is discarded on exit — the image **bytes**
  travel in `RenderedImage`, nothing is left on disk.
- Run `sanitize_markdown` on the result (as today).
- Return `(canonical_markdown, images)`.

> Identical image bytes → identical `<sha>.png` reference regardless of page
> number, so the canonical markdown — and therefore the article's `content_hash`
> — is stable across cover-page insertions and dedupes repeated figures.

**`render_segments(pdf_bytes, segments) -> list[tuple[str, list[RenderedImage]]]`**
- Open the PDF once (as today), call `_render_segment(doc, seg)` for each segment,
  and return the per-segment `(markdown, images)` tuples aligned with `segments`.

**`segment_to_markdown(pdf_bytes, segment) -> str`** (kept for existing unit tests)
- Opens the PDF, calls `_render_segment`, and returns only the markdown (dropping
  the images). Existing callers/tests are unaffected.

### Persistence (extend `process_article_result` in `firecrawl.py`)

Add one optional parameter:

```python
async def process_article_result(self, db, source_id, run_id, url,
    markdown_content, doc_html, toc_entry_id, sort_order, title,
    change_status=None, diff_text=None, topic_key=None,
    pdf_images: list["RenderedImage"] | None = None) -> str:
```

- Hashing/diffing is unchanged — `content_hash` is computed on the passed
  `markdown_content` (the canonical form with `<sha>.png` references).
- The existing web block stays `if doc_html:`. Add a sibling
  `elif pdf_images:` on the new/updated path, after the `Article` row exists:
  1. `article_img_dir = media_dir/<article.id>`; **clear it**
     (`shutil.rmtree(..., ignore_errors=True)` then `os.makedirs`) so a re-render
     leaves only current figures.
  2. For each `RenderedImage`: write `data` to `<article_img_dir>/<filename>`,
     create an `ArticleImage` row (`original_url = f"pdf:{filename}"` to satisfy
     the NOT-NULL column, `local_filename = filename`,
     `local_path = served_url`, `alt_text = alt`, `sort_order = i`), and rewrite
     `](<filename>)` → `](<media_url_prefix>/<article.id>/<filename>)` in
     `markdown_content`.
  3. The existing `article.content_markdown = markdown_content` assignment then
     stores the **served** form — same as the web path.
- PDF calls pass `doc_html=""` and `pdf_images=<images>`.

### Wiring (`run_pdf_extraction` in `pdf_import.py`)

- `render_segments` now returns `(md, images)` per segment. Build
  `article_inputs` to carry the images alongside the existing fields; keep the
  `articles_total = count of segments with non-empty md`.
- Pass `pdf_images=images` (and `doc_html=""`) into each
  `service.process_article_result(...)` call.

### Incremental safety
`content_hash` is computed on the canonical markdown (`<sha>.png` refs), which is
independent of `article.id` and page numbers. A re-run with identical figures →
identical hash → "unchanged": the image block is skipped and the existing files
under `media_dir/<article.id>/` are left untouched. A cover-page insertion that
shifts pages but keeps a figure → still "unchanged".

---

## Part 2 — Media GC (global, both PDF and web)

`ArticleImage` rows cascade-delete with their article, but the on-disk
`media_dir/<article.id>/` directory is left behind on a **hard delete** (article,
or a source/product/vendor delete that cascades to articles). Add a global sweep
that reconciles the media volume against the live `articles` table — this catches
every delete path regardless of which route performed it.

### Service (`app/services/media_gc.py`)

```python
async def gc_orphaned_media(db, media_dir: str) -> int:
    """Remove media_dir/<uuid>/ directories whose article no longer exists.
    Returns the count of directories removed."""
```

- List immediate entries of `media_dir`. For each entry that is a directory and
  whose name parses as a UUID, collect the candidate article ids.
- Query the `articles` table for which of those ids still exist
  (`SELECT id FROM articles WHERE id IN (...)`).
- `shutil.rmtree(media_dir/<id>, ignore_errors=True)` for each directory whose
  UUID is **not** a current article. Non-UUID entries are ignored (defensive).
- Return the number removed.

This mirrors `export_retention.purge_expired_exports`'s orphan-directory sweep.

### Scheduling (`app/services/scheduling.py`)

Mirror the existing export-purge hook in `tick()`:

- Add `_MEDIA_GC_INTERVAL = timedelta(hours=1)` and `_last_media_gc: datetime | None`.
- In `tick()`, when due, call
  `await gc_orphaned_media(db, settings.media_dir)` and update `_last_media_gc`.

No new settings — `settings.media_dir` already exists, and an hourly cadence
matches the export sweep.

---

## Testing

Backend (`tests/`, existing conventions; PDFs generated in-test with PyMuPDF +
a `fitz.Pixmap` for a real raster image):

- **Canonicalization** (`_render_segment` / `render_segments`): a one-image PDF
  yields markdown whose marker is `![](<sha>.png)` where `<sha>` equals
  `sha256(file_bytes)[:16]`, and one `RenderedImage` carrying those bytes; a
  no-image PDF yields no `RenderedImage` and no markers; two identical images in a
  segment dedupe to one `RenderedImage`.
- **Persist end-to-end** (`run_pdf_extraction`): a PDF with an image creates the
  article with `content_markdown` containing `/media/<article.id>/<sha>.png`, an
  `ArticleImage` row, and the file present at `media_dir/<article.id>/<sha>.png`.
- **Incremental stability**: re-running the same PDF leaves the section
  `unchanged`; a cover-page-shifted variant with the same figure is also
  `unchanged` (image content hash keeps `content_hash` stable).
- **Per-run clear**: an updated run (figure changed) leaves only the new image in
  `media_dir/<article.id>/`.
- **Web regression**: existing `doc_html` image tests still pass (the new param
  defaults to `None`; the `elif` never fires for web).
- **Media GC**: a `media_dir` containing one dir for an existing article and one
  for a deleted (absent) article id → `gc_orphaned_media` removes only the orphan
  and returns 1; non-UUID directories are left untouched.

`media_dir` / `media_url_prefix` are overridden to a `tmp_path` in tests that
write files.

---

## Non-goals

- No OCR, figure captioning, or image re-encoding/optimization (PNG as
  pymupdf4llm emits).
- No change to web image behavior beyond the additive `pdf_images` branch.
- The media GC reconciles directories against articles only; it does not validate
  individual files within a live article's directory (the per-run clear already
  keeps those current).
