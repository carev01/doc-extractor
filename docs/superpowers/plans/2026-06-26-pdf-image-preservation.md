# PDF Image Preservation + Media GC Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract images from PDFs during conversion, store them on the media volume and serve them over HTTP (mirroring the web image pipeline with content-hash filenames), and add a global media GC that removes orphaned `media/<article_id>/` directories left by hard deletes (PDF and web).

**Architecture:** `pdf_import.render_segments` renders each segment with `write_images=True`, content-addresses each image to `<sha>.png`, and returns `(canonical_markdown, [RenderedImage])`. `process_article_result` gains an additive `pdf_images` branch — a sibling of its existing `if doc_html:` web block — that writes the images under `media/<article.id>/`, creates `ArticleImage` rows, and rewrites the markdown to served `/media` URLs, keeping the hash on the canonical form. A new `media_gc.gc_orphaned_media` reconciles `media/<uuid>/` directories against the live `articles` table, hooked into the hourly `scheduling.tick()`.

**Tech Stack:** Python, FastAPI, SQLAlchemy (async asyncpg), PyMuPDF (`fitz`) + `pymupdf4llm`, pytest.

## Global Constraints

- Backend settings prefix `DOCEXTRACTOR_`; tests use `docextractor_test` DB (`settings.database_url.rsplit("/",1)[0] + "/docextractor_test"`). Run tests with `python3 -m pytest` (binary is `python3`).
- Image filenames are content-addressed: `<sha>.png` where `sha = hashlib.sha256(image_bytes).hexdigest()[:16]`. The canonical markdown reference is the bare `<sha>.png`; the served reference is `<media_url_prefix>/<article.id>/<sha>.png`.
- `content_hash` MUST be computed on the canonical markdown (with `<sha>.png` refs), exactly as the web path hashes its pre-rewrite markdown. Served `/media/<article.id>/…` URLs must never affect diffs.
- Reuse the existing `ArticleImage` model (`app/models/image.py`): fields `article_id, original_url, local_filename, local_path, alt_text, file_size_bytes, sort_order`. `original_url` is NOT NULL.
- `media_dir` is served as `StaticFiles` at `media_url_prefix` (default `/media`), mounted in `app/main.py`. Both default to `media` / `/media`.
- The `pdf_images` parameter is additive and defaults to `None`; the web `doc_html` path must be unchanged (no regression).
- Media GC reconciles directories against articles only; hourly cadence mirrors the export-retention sweep. No new settings (`settings.media_dir` exists).
- Tests generate PDFs in-process with PyMuPDF; a real raster image is made with `fitz.Pixmap`. Tests that write files override `settings.media_dir` (and `media_url_prefix` if asserting URLs) to a `tmp_path`.
- All commands run from `backend/`.

---

## Task 1: Render + content-address PDF images

**Files:**
- Modify: `backend/app/services/pdf_import.py` (`RenderedImage`, `_render_segment`, `render_segments`, `segment_to_markdown`, and the `run_pdf_extraction` unpack site)
- Test: `backend/tests/test_pdf_images_render.py`

**Interfaces:**
- Produces:
  - `@dataclass RenderedImage: filename: str; data: bytes; alt: str`
  - `_render_segment(doc, segment) -> tuple[str, list[RenderedImage]]`
  - `render_segments(pdf_bytes, segments) -> list[tuple[str, list[RenderedImage]]]`
  - `segment_to_markdown(pdf_bytes, segment) -> str` (unchanged signature; returns md only)
  - Consumed by Task 2 (`run_pdf_extraction` passes the images list to `process_article_result`).

**Context:** `pymupdf4llm.to_markdown(doc, pages=..., write_images=True, image_path=<dir>, image_format="png")` writes `-<page>-<index>.png` files into `<dir>` and emits markdown markers `![](<dir>/-p-i.png)`. `hashlib`, `os`, `fitz`, `pymupdf4llm`, `sanitize_markdown` are already imported in `pdf_import.py`; add `import re` and `import tempfile`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_pdf_images_render.py`:

```python
import hashlib
import os
import re
import sys

import fitz

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.pdf_import import (
    Segment, RenderedImage, render_segments, segment_to_markdown,
)


def _img_pixmap(rgb=(255, 0, 0)):
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 64, 64))
    pix.set_rect(pix.irect, rgb)
    return pix


def _pdf_one_image() -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Heading above the figure")
    page.insert_image(fitz.Rect(72, 100, 200, 200), pixmap=_img_pixmap())
    page.insert_text((72, 320), "Caption below the figure")
    return doc.tobytes()


def _pdf_no_image() -> bytes:
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "Just text, no figures.")
    return doc.tobytes()


def test_image_is_content_addressed_and_referenced():
    pdf = _pdf_one_image()
    seg = Segment("Doc", 1, 0, 0, ["Doc"])
    [(md, images)] = render_segments(pdf, [seg])
    assert len(images) == 1
    img = images[0]
    # filename is sha256(bytes)[:16] + .png
    assert img.filename == hashlib.sha256(img.data).hexdigest()[:16] + ".png"
    # markdown references the bare canonical filename (no temp path, no /media)
    assert f"]({img.filename})" in md
    assert "/tmp" not in md and "/media" not in md
    # surrounding text preserved
    assert "Heading above the figure" in md and "Caption below the figure" in md


def test_no_image_segment_yields_no_rendered_images():
    pdf = _pdf_no_image()
    seg = Segment("Doc", 1, 0, 0, ["Doc"])
    [(md, images)] = render_segments(pdf, [seg])
    assert images == []
    assert "![" not in md


def _pdf_two_identical_images() -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 60), "Top")
    page.insert_image(fitz.Rect(72, 80, 180, 180), pixmap=_img_pixmap())
    page.insert_text((72, 200), "Middle")
    page.insert_image(fitz.Rect(72, 220, 180, 320), pixmap=_img_pixmap())
    return doc.tobytes()


def test_identical_images_dedupe_to_one_rendered_image():
    # Two placements of the same image bytes collapse to a single RenderedImage
    # (content-addressed), regardless of how many markers pymupdf4llm emits.
    pdf = _pdf_two_identical_images()
    seg = Segment("Doc", 1, 0, 0, ["Doc"])
    [(md, images)] = render_segments(pdf, [seg])
    assert len(images) == 1                               # same bytes → one image
    assert f"]({images[0].filename})" in md              # referenced by canonical name


def test_segment_to_markdown_still_returns_str():
    pdf = _pdf_one_image()
    md = segment_to_markdown(pdf, Segment("Doc", 1, 0, 0, ["Doc"]))
    assert isinstance(md, str)
    assert "Heading above the figure" in md
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_pdf_images_render.py -v`
Expected: FAIL — `ImportError: cannot import name 'RenderedImage'`.

- [ ] **Step 3: Implement the rendering changes**

In `backend/app/services/pdf_import.py`, add `import re` and `import tempfile` to the top imports. Add the dataclass near `Segment`:

```python
@dataclass
class RenderedImage:
    filename: str   # content-addressed: "<sha16>.png"
    data: bytes
    alt: str
```

Replace `_render_segment`, `segment_to_markdown`, and `render_segments` with:

```python
_IMG_MARKER = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<target>[^)]+)\)")


def _render_segment(doc: "fitz.Document", segment: Segment) -> tuple[str, list[RenderedImage]]:
    """Render a segment to clean markdown, content-addressing any images.

    Images are written to a temp dir by pymupdf4llm, then each marker is rewritten
    to a bare ``<sha>.png`` canonical reference and the bytes collected — so the
    markdown is stable across runs/page-shifts and identical figures dedupe."""
    pages = list(range(segment.page_start, segment.page_end + 1))
    images: list[RenderedImage] = []
    seen: dict[str, str] = {}  # original target -> canonical filename
    with tempfile.TemporaryDirectory() as image_dir:
        md = pymupdf4llm.to_markdown(
            doc, pages=pages, write_images=True,
            image_path=image_dir, image_format="png",
        ) or ""

        def _replace(m: "re.Match") -> str:
            target = m.group("target")
            alt = m.group("alt")
            path = os.path.join(image_dir, os.path.basename(target))
            if not os.path.isfile(path):
                return m.group(0)  # not a written image — leave untouched
            if target in seen:
                return f"![{alt}]({seen[target]})"
            data = open(path, "rb").read()
            filename = hashlib.sha256(data).hexdigest()[:16] + ".png"
            seen[target] = filename
            if all(img.filename != filename for img in images):
                images.append(RenderedImage(filename=filename, data=data, alt=alt))
            return f"![{alt}]({filename})"

        md = _IMG_MARKER.sub(_replace, md)
    return sanitize_markdown(md), images


def segment_to_markdown(pdf_bytes: bytes, segment: Segment) -> str:
    """Render a segment's page range to clean markdown (without images)."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        md, _images = _render_segment(doc, segment)
        return md
    finally:
        doc.close()


def render_segments(
    pdf_bytes: bytes, segments: list[Segment]
) -> list[tuple[str, list[RenderedImage]]]:
    """Render every segment, opening the PDF once. Returns (markdown, images)
    per segment, aligned with ``segments``."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        return [_render_segment(doc, seg) for seg in segments]
    finally:
        doc.close()
```

- [ ] **Step 4: Keep `run_pdf_extraction` green (unpack the new tuple)**

`run_pdf_extraction` currently does `rendered = render_segments(...)` then
`article_inputs.append((toc.id, i, seg.title, topic_key, url, rendered[i]))` and
`run.articles_total = sum(1 for inp in article_inputs if inp[5].strip())`.
`rendered[i]` is now a `(md, images)` tuple. For THIS task, use only the markdown
so existing run tests stay green (Task 2 wires the images). Change that line to:

```python
        article_inputs.append((toc.id, i, seg.title, topic_key, url, rendered[i][0]))
```

(Leave everything else in `run_pdf_extraction` unchanged.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_pdf_images_render.py tests/test_pdf_to_markdown.py tests/test_pdf_run_extraction.py -v`
Expected: PASS — the new render tests, the existing markdown tests, and the existing run-extraction tests (which now read `rendered[i][0]`).

- [ ] **Step 6: Commit**

```bash
git add app/services/pdf_import.py tests/test_pdf_images_render.py
git commit -m "feat(pdf): render and content-address PDF images"
```

---

## Task 2: Persist PDF images via `process_article_result`

**Files:**
- Modify: `backend/app/services/firecrawl.py` (`process_article_result` signature + a new `elif pdf_images:` block; add `import shutil`)
- Modify: `backend/app/services/pdf_import.py` (`run_pdf_extraction` passes `pdf_images`)
- Test: `backend/tests/test_pdf_images_persist.py`

**Interfaces:**
- Consumes: `RenderedImage` (Task 1), the existing `ArticleImage` model.
- Produces: `process_article_result(..., pdf_images: list | None = None)` writes images under `media/<article.id>/`, creates `ArticleImage` rows, and rewrites the stored markdown to served URLs.

**Context:** In `process_article_result`, `media_root = os.path.abspath(settings.media_dir)` is computed (~line 648); the web image block is `if doc_html:` (~line 712-760); `article.content_markdown = markdown_content` (~line 762) stores the final (rewritten) markdown for both new and updated paths; `content_hash` was computed earlier (~line 599) on the canonical markdown. On the updated path, prior `ArticleImage` rows are already deleted (~line 680-685). `ArticleImage` is imported at the top of the file.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_pdf_images_persist.py`:

```python
import os
import sys
import uuid

import fitz
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession, async_sessionmaker, create_async_engine,
)
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core.database import Base
from app.models import Vendor, Product, DocumentationSource, ExtractionRun, Article
from app.models.image import ArticleImage
from app.services.firecrawl import FirecrawlService
from app.services.pdf_import import run_pdf_extraction, pdf_path_for

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def factory():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    f = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield f
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


def _pix(rgb):
    p = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 64, 64)); p.set_rect(p.irect, rgb)
    return p


def _pdf(color=(255, 0, 0)) -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Section with a figure")
    page.insert_image(fitz.Rect(72, 100, 200, 200), pixmap=_pix(color))
    doc.set_toc([[1, "Figure section", 1]])
    return doc.tobytes()


async def _source(factory, tmp_path) -> uuid.UUID:
    settings.media_dir = str(tmp_path / "media")
    settings.pdf_dir = str(tmp_path / "pdf")
    os.makedirs(settings.pdf_dir, exist_ok=True)
    async with factory() as s:
        v = Vendor(name="V"); s.add(v); await s.flush()
        p = Product(vendor_id=v.id, name="P"); s.add(p); await s.flush()
        src = DocumentationSource(product_id=p.id, name="M",
                                  base_url="file://x.pdf", source_type="pdf")
        s.add(src); await s.commit()
        return src.id


async def _run(factory, sid) -> uuid.UUID:
    svc = FirecrawlService()
    async with factory() as s:
        src = await s.get(DocumentationSource, sid)
        run = ExtractionRun(source_id=sid); s.add(run); await s.flush()
        rid = run.id
        await run_pdf_extraction(svc, s, src, run, rid)
        await s.commit()
    return rid


async def test_pdf_image_persisted_and_served(factory, tmp_path):
    sid = await _source(factory, tmp_path)
    with open(pdf_path_for(sid, settings.pdf_dir), "wb") as fh:
        fh.write(_pdf())
    await _run(factory, sid)

    async with factory() as s:
        art = (await s.execute(
            select(Article).where(Article.source_id == sid))).scalar_one()
        imgs = (await s.execute(
            select(ArticleImage).where(ArticleImage.article_id == art.id))).scalars().all()
        assert len(imgs) == 1
        fname = imgs[0].local_filename
        served = f"{settings.media_url_prefix}/{art.id}/{fname}"
        assert imgs[0].local_path == served
        assert served in art.content_markdown          # rewritten to served URL
        assert os.path.isfile(os.path.join(settings.media_dir, str(art.id), fname))


async def test_rerun_same_image_is_unchanged(factory, tmp_path):
    sid = await _source(factory, tmp_path)
    with open(pdf_path_for(sid, settings.pdf_dir), "wb") as fh:
        fh.write(_pdf())
    await _run(factory, sid)
    rid2 = await _run(factory, sid)
    async with factory() as s:
        r = await s.get(ExtractionRun, rid2)
        assert r.articles_unchanged == 1 and r.articles_extracted == 0


async def test_changed_image_clears_old_file(factory, tmp_path):
    sid = await _source(factory, tmp_path)
    with open(pdf_path_for(sid, settings.pdf_dir), "wb") as fh:
        fh.write(_pdf(color=(255, 0, 0)))
    await _run(factory, sid)
    with open(pdf_path_for(sid, settings.pdf_dir), "wb") as fh:
        fh.write(_pdf(color=(0, 0, 255)))   # different image bytes → new sha
    await _run(factory, sid)
    async with factory() as s:
        art = (await s.execute(
            select(Article).where(Article.source_id == sid))).scalar_one()
    # Only the current image remains in the article's media dir.
    files = os.listdir(os.path.join(settings.media_dir, str(art.id)))
    assert len(files) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_pdf_images_persist.py -v`
Expected: FAIL — no `ArticleImage` row / file is created (the markdown still carries the bare `<sha>.png` ref, no `/media` URL), so the assertions fail.

- [ ] **Step 3: Add the `pdf_images` parameter and branch in `process_article_result`**

In `backend/app/services/firecrawl.py`, add `import shutil` to the top imports. Change the signature (add the last parameter):

```python
        change_status: str | None = None,
        diff_text: str | None = None,
        topic_key: str | None = None,
        pdf_images: list | None = None,
    ) -> str:
```

Immediately AFTER the `if doc_html:` web image block (the line `markdown_content = markdown_content.replace(src, served_url)` ends it) and BEFORE `article.content_markdown = markdown_content`, add the sibling branch:

```python
        elif pdf_images:
            # PDF source images: written by render_segments as content-addressed
            # bytes. Clear the article's media dir so only current figures remain,
            # write each image, record an ArticleImage row, and rewrite the bare
            # canonical "<sha>.png" reference to the served /media URL. The hash was
            # already taken on the canonical markdown, so served paths don't diff.
            article_img_dir = os.path.join(media_root, str(article.id))
            shutil.rmtree(article_img_dir, ignore_errors=True)
            os.makedirs(article_img_dir, exist_ok=True)
            for i, img in enumerate(pdf_images):
                with open(os.path.join(article_img_dir, img.filename), "wb") as fh:
                    fh.write(img.data)
                served_url = f"{settings.media_url_prefix}/{article.id}/{img.filename}"
                db.add(ArticleImage(
                    article_id=article.id,
                    original_url=f"pdf:{img.filename}",
                    local_filename=img.filename,
                    local_path=served_url,
                    alt_text=img.alt or None,
                    file_size_bytes=len(img.data),
                    sort_order=i,
                ))
                markdown_content = markdown_content.replace(
                    f"]({img.filename})", f"]({served_url})"
                )
```

- [ ] **Step 4: Pass `pdf_images` from `run_pdf_extraction`**

In `backend/app/services/pdf_import.py`, `run_pdf_extraction` builds `article_inputs`
and loops over them. Update both the tuple build and the call so the images ride
along:

Change the append (it currently stores `rendered[i][0]`) to keep both md and images:

```python
        article_inputs.append((toc.id, i, seg.title, topic_key, url, rendered[i][0], rendered[i][1]))
```

Update the `articles_total` line (tuple grew, md is still index 5):

```python
    run.articles_total = sum(1 for inp in article_inputs if inp[5].strip())
```

(unchanged — index 5 is still the markdown).

Update the persist loop to unpack and pass `pdf_images`:

```python
    for toc_id, sort_order, title, topic_key, url, md, images in article_inputs:
        await service.process_article_result(
            db, source.id, run_pk, url=url, markdown_content=md, doc_html="",
            toc_entry_id=toc_id, sort_order=sort_order, title=title,
            change_status=None, topic_key=topic_key, pdf_images=images,
        )
```

- [ ] **Step 5: Run tests to verify they pass (and no web regression)**

Run: `python3 -m pytest tests/test_pdf_images_persist.py tests/test_pdf_run_extraction.py tests/test_incremental.py tests/test_reconcile_removals.py -v`
Expected: PASS — PDF image persistence, existing PDF run tests, and the web image/incremental tests (the `pdf_images=None` default leaves the `doc_html` path untouched).

- [ ] **Step 6: Commit**

```bash
git add app/services/firecrawl.py app/services/pdf_import.py tests/test_pdf_images_persist.py
git commit -m "feat(pdf): persist PDF images to media and serve them"
```

---

## Task 3: Media GC service

**Files:**
- Create: `backend/app/services/media_gc.py`
- Test: `backend/tests/test_media_gc.py`

**Interfaces:**
- Produces: `async def gc_orphaned_media(db, media_dir: str) -> int` — removes `media_dir/<uuid>/` directories whose article no longer exists; returns the count removed. Consumed by Task 4.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_media_gc.py`:

```python
import os
import sys
import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession, async_sessionmaker, create_async_engine,
)
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core.database import Base
from app.models import Vendor, Product, DocumentationSource, ExtractionRun, Article
from app.services.media_gc import gc_orphaned_media

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def factory():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    f = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield f
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def _article(factory) -> uuid.UUID:
    async with factory() as s:
        v = Vendor(name="V"); s.add(v); await s.flush()
        p = Product(vendor_id=v.id, name="P"); s.add(p); await s.flush()
        src = DocumentationSource(product_id=p.id, name="M", base_url="https://d")
        s.add(src); await s.flush()
        run = ExtractionRun(source_id=src.id); s.add(run); await s.flush()
        art = Article(source_id=src.id, title="T", source_url="https://d/a",
                      topic_key="a", content_markdown="x")
        s.add(art); await s.commit()
        return art.id


def _mkdir(media_dir, name):
    d = os.path.join(media_dir, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "f.png"), "wb") as fh:
        fh.write(b"x")
    return d


async def test_gc_removes_only_orphan_dirs(factory, tmp_path):
    media = str(tmp_path)
    live_id = await _article(factory)
    live = _mkdir(media, str(live_id))
    orphan = _mkdir(media, str(uuid.uuid4()))    # no such article
    nonuuid = _mkdir(media, "not-a-uuid")        # left untouched

    async with factory() as s:
        removed = await gc_orphaned_media(s, media)

    assert removed == 1
    assert os.path.isdir(live)
    assert not os.path.exists(orphan)
    assert os.path.isdir(nonuuid)


async def test_gc_handles_missing_media_dir(factory, tmp_path):
    async with factory() as s:
        removed = await gc_orphaned_media(s, str(tmp_path / "does-not-exist"))
    assert removed == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_media_gc.py -v`
Expected: FAIL — `ModuleNotFoundError: app.services.media_gc`.

- [ ] **Step 3: Implement**

Create `backend/app/services/media_gc.py`:

```python
"""Media GC — remove media_dir/<article_id>/ directories whose article no longer
exists (orphans left by hard deletes of articles / sources / products / vendors).
Reconciles the media volume against the live articles table, so it catches every
delete path regardless of which route performed it."""
import logging
import os
import shutil
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article import Article

logger = logging.getLogger(__name__)


async def gc_orphaned_media(db: AsyncSession, media_dir: str) -> int:
    """Remove media_dir/<uuid>/ directories with no matching article. Returns the
    number removed. Non-UUID entries are ignored."""
    if not os.path.isdir(media_dir):
        return 0

    candidates: dict[uuid.UUID, str] = {}
    for name in os.listdir(media_dir):
        path = os.path.join(media_dir, name)
        if not os.path.isdir(path):
            continue
        try:
            candidates[uuid.UUID(name)] = path
        except ValueError:
            continue  # not an article-id directory — leave it alone

    if not candidates:
        return 0

    existing = set(
        (await db.execute(
            select(Article.id).where(Article.id.in_(list(candidates)))
        )).scalars()
    )

    removed = 0
    for art_id, path in candidates.items():
        if art_id not in existing:
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
    if removed:
        logger.info("media GC removed %d orphaned image dir(s)", removed)
    return removed
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_media_gc.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/media_gc.py tests/test_media_gc.py
git commit -m "feat(media): gc_orphaned_media reconciles media dirs against articles"
```

---

## Task 4: Hook media GC into the scheduler tick

**Files:**
- Modify: `backend/app/services/scheduling.py` (import, interval/state, `tick()` call)
- Test: `backend/tests/test_media_gc_scheduled.py`

**Interfaces:**
- Consumes: `gc_orphaned_media` (Task 3).
- Produces: `tick()` runs the media GC at most hourly.

**Context:** `scheduling.py` already runs `purge_expired_exports` on an hourly interval inside `tick()` using module state `_last_export_purge` and `_EXPORT_PURGE_INTERVAL = timedelta(hours=1)`. Mirror that exactly. `timedelta`, `datetime`, `settings`, `select` are already imported there.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_media_gc_scheduled.py`:

```python
import os
import sys
import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession, async_sessionmaker, create_async_engine,
)
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core.database import Base
import app.services.scheduling as scheduling

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def factory():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    f = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield f
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def test_tick_sweeps_orphan_media(factory, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "media_dir", str(tmp_path))
    scheduling._last_media_gc = None     # force the GC to be due this tick
    orphan = os.path.join(str(tmp_path), str(uuid.uuid4()))
    os.makedirs(orphan, exist_ok=True)

    async with factory() as s:
        await scheduling.tick(s)

    assert not os.path.exists(orphan)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_media_gc_scheduled.py -v`
Expected: FAIL — `AttributeError: module 'app.services.scheduling' has no attribute '_last_media_gc'` (or the orphan is not removed).

- [ ] **Step 3: Implement the hook**

In `backend/app/services/scheduling.py`, add the import near the other service imports:

```python
from app.services.media_gc import gc_orphaned_media
```

Add module state next to `_last_export_purge` (after the `_EXPORT_PURGE_INTERVAL` block):

```python
_MEDIA_GC_INTERVAL = timedelta(hours=1)
_last_media_gc: datetime | None = None
```

In `tick()`, right after the export-purge block (after `_last_export_purge = now`), add:

```python
    global _last_media_gc
    if _last_media_gc is None or (now - _last_media_gc) >= _MEDIA_GC_INTERVAL:
        await gc_orphaned_media(db, settings.media_dir)
        _last_media_gc = now
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_media_gc_scheduled.py tests/test_scheduler.py -v`
Expected: PASS — the new test and the existing scheduler tests.

- [ ] **Step 5: Commit**

```bash
git add app/services/scheduling.py tests/test_media_gc_scheduled.py
git commit -m "feat(media): run media GC hourly from the scheduler tick"
```

---

## Final verification

- [ ] **Backend suite**

Run: `cd backend && python3 -m pytest -q`
Expected: all tests pass.
