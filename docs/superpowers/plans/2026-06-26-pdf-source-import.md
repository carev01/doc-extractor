# PDF Source Import Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Import documentation from a PDF (URL or upload), convert it to per-article markdown split on natural content boundaries, as a first-class source with full incremental/versioning parity; and move the CSV-import UI from the Dashboard to the Vendors view.

**Architecture:** A new `source_type` discriminates `web` vs `pdf` sources. `FirecrawlService.extract_source` branches at the top: a `pdf` source delegates to a new `services/pdf_import.py` pipeline that acquires the PDF, segments it (outline-first; LLM/heuristic/single-segment fallback), converts each segment to markdown, and persists ordinary `Article` rows through the **existing** `process_article_result` diff/versioning machinery — so export, browse, changelog, and scheduling all work unchanged.

**Tech Stack:** FastAPI, SQLAlchemy (async asyncpg), Alembic, Pydantic v2, PyMuPDF (`fitz`) + `pymupdf4llm`, React 19 + TypeScript + Vite, pytest + httpx.AsyncClient.

## Global Constraints

- Backend settings prefix `DOCEXTRACTOR_`; tests use the `docextractor_test` DB (`settings.database_url.rsplit("/",1)[0] + "/docextractor_test"`). Run tests with `python3 -m pytest` (binary is `python3`, not `python`).
- New columns use the project convention: `source_type` → `String(16), default="web", server_default="web", nullable=False`; `pdf_hash` → `String(64), nullable=True`.
- Alembic current head is `f1e2d3c4b5a6`; the new migration's `down_revision` must be `"f1e2d3c4b5a6"`. A **single** migration adds both new columns.
- Route/integration tests follow `tests/test_job_routes.py`: `pytest.mark.asyncio`, `httpx.AsyncClient(transport=ASGITransport(app=app))`, a per-test fixture that drops+creates `Base.metadata` and overrides `get_db`.
- PDF segmentation reuses the **existing** LLM settings only (`llm_provider`, `llm_base_url`, `llm_api_key`, `llm_model`, `llm_max_tokens`, `llm_fallback_enabled`). No new LLM settings.
- PDF articles persist through the existing `FirecrawlService.process_article_result(db, source_id, run_id, url, markdown_content, doc_html, toc_entry_id, sort_order, title, change_status=None, diff_text=None, topic_key=None) -> "new"|"updated"|"unchanged"|"empty"`. Pass `doc_html=""` and `change_status=None` (hash-based diffing).
- Frontend gate: `cd frontend && npm run build` (tsc + vite) must pass; `npm run lint` must stay at **0 problems** (the current baseline after the lint-cleanup PR). `node_modules` is already installed — never run `npm install`. Use `import type` for type-only imports.
- Non-goals (do not build): OCR/scanned-PDF support, per-figure PDF image extraction.
- All backend commands run from `backend/`; frontend from `frontend/`.

---

## Task 1: Dependencies, settings, columns & migration

**Files:**
- Modify: `backend/requirements.txt`
- Modify: `backend/app/core/config.py`
- Modify: `backend/app/models/source.py`
- Modify: `backend/app/models/extraction_run.py`
- Create: `backend/alembic/versions/b2c3d4e5f6a7_add_pdf_source.py`
- Test: `backend/tests/test_pdf_model_and_config.py`

**Interfaces:**
- Produces: `DocumentationSource.source_type: str` (default `"web"`), `ExtractionRun.pdf_hash: str | None`, `settings.pdf_dir: str`, `settings.pdf_max_upload_bytes: int`. Consumed by Tasks 6, 8, 10.

- [ ] **Step 1: Install the PDF libraries and pin them**

Run: `python3 -m pip install "pymupdf4llm==0.0.17" "PyMuPDF==1.24.14"`
Expected: installs cleanly. Then add to `backend/requirements.txt` (after `pypdf==5.1.0`):

```
PyMuPDF==1.24.14
pymupdf4llm==0.0.17
```

> If those exact versions fail to resolve on this platform, install the latest compatible `pymupdf4llm` and `PyMuPDF`, then pin the versions that actually installed (`python3 -c "import fitz, pymupdf4llm; print(fitz.__doc__)"` to confirm import works) and write those into requirements.txt.

- [ ] **Step 2: Write the failing test**

Create `backend/tests/test_pdf_model_and_config.py`:

```python
"""source_type / pdf_hash columns + pdf settings."""
import os
import sys

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession, async_sessionmaker, create_async_engine,
)
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core.database import Base
from app.models import Vendor, Product, DocumentationSource, ExtractionRun

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


async def test_source_type_defaults_web_and_pdf_hash_nullable(factory):
    async with factory() as s:
        v = Vendor(name="V"); s.add(v); await s.flush()
        p = Product(vendor_id=v.id, name="P"); s.add(p); await s.flush()
        src = DocumentationSource(product_id=p.id, name="D", base_url="https://d")
        s.add(src); await s.flush()
        run = ExtractionRun(source_id=src.id)
        s.add(run); await s.commit()
        assert src.source_type == "web"
        assert run.pdf_hash is None


def test_pdf_settings_present():
    assert settings.pdf_dir
    assert settings.pdf_max_upload_bytes >= 1024 * 1024
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python3 -m pytest tests/test_pdf_model_and_config.py -v`
Expected: FAIL — `AttributeError`/`TypeError` for `source_type` / `pdf_hash`, and `settings.pdf_dir` missing.

- [ ] **Step 4: Add the settings**

In `backend/app/core/config.py`, after the export/media settings block, add:

```python
    # PDF source import — uploaded PDFs live on a local volume (a PVC in k8s),
    # mirroring media_dir/export_dir. Uploads larger than the cap are rejected.
    pdf_dir: str = "pdf_uploads"
    pdf_max_upload_bytes: int = 100 * 1024 * 1024  # 100 MiB
```

- [ ] **Step 5: Add the columns**

In `backend/app/models/source.py`, after `base_url` (and before `url_template`), add:

```python
    # "web" (crawled) | "pdf" (imported from a PDF URL or upload).
    source_type: Mapped[str] = mapped_column(
        String(16), default="web", server_default="web", nullable=False
    )
```

In `backend/app/models/extraction_run.py`, after `articles_resumed`, add:

```python
    # SHA-256 of the PDF bytes for a pdf source (NULL for web runs); lets a
    # re-run fast-path to "all unchanged" when the PDF is byte-identical.
    pdf_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
```

- [ ] **Step 6: Create the migration**

Create `backend/alembic/versions/b2c3d4e5f6a7_add_pdf_source.py`:

```python
"""add documentation_sources.source_type + extraction_runs.pdf_hash

Revision ID: b2c3d4e5f6a7
Revises: f1e2d3c4b5a6
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, Sequence[str], None] = "f1e2d3c4b5a6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "documentation_sources",
        sa.Column("source_type", sa.String(16), nullable=False, server_default="web"),
    )
    op.add_column(
        "extraction_runs",
        sa.Column("pdf_hash", sa.String(64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("extraction_runs", "pdf_hash")
    op.drop_column("documentation_sources", "source_type")
```

- [ ] **Step 7: Run test to verify it passes**

Run: `python3 -m pytest tests/test_pdf_model_and_config.py -v`
Expected: PASS (both tests).

- [ ] **Step 8: Commit**

```bash
git add requirements.txt app/core/config.py app/models/source.py app/models/extraction_run.py alembic/versions/b2c3d4e5f6a7_add_pdf_source.py tests/test_pdf_model_and_config.py
git commit -m "feat(pdf): add source_type/pdf_hash columns, pdf settings, deps"
```

---

## Task 2: `derive_pdf_topic_key`

**Files:**
- Modify: `backend/app/services/versioning.py`
- Test: `backend/tests/test_pdf_topic_key.py`

**Interfaces:**
- Produces: `derive_pdf_topic_key(path: list[str]) -> str` — a stable slug from an outline path. Consumed by Task 8.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_pdf_topic_key.py`:

```python
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.versioning import derive_pdf_topic_key


def test_slug_from_path():
    assert derive_pdf_topic_key(["Chapter 1", "Installation"]) == "chapter-1/installation"


def test_collapses_whitespace_and_punctuation():
    assert derive_pdf_topic_key(["A &  B!!", "C/D"]) == "a-b/c-d"


def test_empty_path_is_stable():
    assert derive_pdf_topic_key([]) == "document"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_pdf_topic_key.py -v`
Expected: FAIL — `ImportError: cannot import name 'derive_pdf_topic_key'`.

- [ ] **Step 3: Implement**

In `backend/app/services/versioning.py`, add at module level (with `import re` at the top if not present):

```python
def _slug(text: str) -> str:
    """Lowercase, keep alphanumerics, collapse everything else to single hyphens."""
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s


def derive_pdf_topic_key(path: list[str]) -> str:
    """Stable topic key for a PDF article from its outline path (ancestor titles +
    own title). Slugged per segment and joined with "/" so re-converting the same
    PDF yields the same key — which keeps incremental diffs stable. Empty path
    (single-segment whole-document fallback) maps to "document"."""
    parts = [_slug(p) for p in path if _slug(p)]
    return "/".join(parts) if parts else "document"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_pdf_topic_key.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/versioning.py tests/test_pdf_topic_key.py
git commit -m "feat(pdf): derive_pdf_topic_key from outline path"
```

---

## Task 3: `Segment` + outline-first `segment_pdf`

**Files:**
- Create: `backend/app/services/pdf_import.py`
- Test: `backend/tests/test_pdf_segment.py`

**Interfaces:**
- Produces: `Segment` dataclass `(title: str, level: int, page_start: int, page_end: int, path: list[str])` (pages are 0-based, inclusive). `segment_pdf(pdf_bytes: bytes) -> list[Segment]`. Consumed by Tasks 4, 5, 7, 8.

**Context:** PyMuPDF `fitz.open(stream=pdf_bytes, filetype="pdf")`; `doc.get_toc(simple=True)` returns `[[level, title, page1based], ...]`. A segment spans from its page to the page just before the next entry at the same-or-higher level; the last entry runs to `doc.page_count - 1`. `path` is the list of ancestor titles plus its own (computed with a level stack).

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_pdf_segment.py`:

```python
import os
import sys

import fitz  # PyMuPDF

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.pdf_import import segment_pdf, Segment


def _pdf_with_outline() -> bytes:
    doc = fitz.open()
    for i in range(4):
        page = doc.new_page()
        page.insert_text((72, 72), f"Body text page {i}")
    # [level, title, 1-based page]
    doc.set_toc([
        [1, "Chapter 1", 1],
        [2, "Installation", 2],
        [1, "Chapter 2", 3],
    ])
    return doc.tobytes()


def _pdf_no_outline() -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Just some text, no bookmarks.")
    return doc.tobytes()


def test_outline_segments_have_correct_ranges_levels_paths():
    segs = segment_pdf(_pdf_with_outline())
    assert [s.title for s in segs] == ["Chapter 1", "Installation", "Chapter 2"]
    assert [s.level for s in segs] == [1, 2, 1]
    # Chapter 1: page 0; Installation: page 1; Chapter 2: pages 2-3
    assert (segs[0].page_start, segs[0].page_end) == (0, 0)
    assert (segs[1].page_start, segs[1].page_end) == (1, 1)
    assert (segs[2].page_start, segs[2].page_end) == (2, 3)
    # Path includes ancestors
    assert segs[1].path == ["Chapter 1", "Installation"]
    assert segs[2].path == ["Chapter 2"]


def test_no_outline_falls_back_to_single_segment():
    segs = segment_pdf(_pdf_no_outline())
    assert len(segs) == 1
    assert segs[0].page_start == 0
    assert segs[0].page_end == 0
    assert isinstance(segs[0], Segment)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_pdf_segment.py -v`
Expected: FAIL — `ModuleNotFoundError: app.services.pdf_import`.

- [ ] **Step 3: Implement the module + outline-first segmentation**

Create `backend/app/services/pdf_import.py`:

```python
"""PDF source import: acquire a PDF, segment it on natural boundaries, convert
each segment to markdown, and persist articles through the existing diff path."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import fitz  # PyMuPDF

logger = logging.getLogger(__name__)


@dataclass
class Segment:
    title: str
    level: int
    page_start: int          # 0-based, inclusive
    page_end: int            # 0-based, inclusive
    path: list[str] = field(default_factory=list)


def _outline_segments(doc: "fitz.Document") -> list[Segment]:
    toc = doc.get_toc(simple=True)  # [[level, title, page1based], ...]
    if not toc:
        return []
    last_page = doc.page_count - 1
    segs: list[Segment] = []
    stack: list[str] = []  # ancestor titles by level
    for i, (level, title, page1) in enumerate(toc):
        start = max(0, page1 - 1)
        # End = page before the next entry at the same-or-higher (<=) level.
        end = last_page
        for nxt_level, _t, nxt_page1 in toc[i + 1:]:
            if nxt_level <= level:
                end = max(start, nxt_page1 - 2)
                break
        stack = stack[: level - 1]
        stack.append(title)
        segs.append(Segment(
            title=title, level=level, page_start=start, page_end=end,
            path=list(stack),
        ))
    return segs


def segment_pdf(pdf_bytes: bytes) -> list[Segment]:
    """Split a PDF into ordered article segments on natural content boundaries.

    Outline-first: when the PDF carries a bookmark outline, each entry is a
    segment spanning to the page before the next same-or-higher-level entry.
    When there is no usable outline this returns a single whole-document segment
    (Tasks 4/5 layer LLM/heuristic fallbacks in front of that)."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        segs = _outline_segments(doc)
        if segs:
            return segs
        # Worst case: one segment for the whole document.
        return [Segment(title="Document", level=1, page_start=0,
                        page_end=max(0, doc.page_count - 1), path=[])]
    finally:
        doc.close()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_pdf_segment.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/pdf_import.py tests/test_pdf_segment.py
git commit -m "feat(pdf): outline-first segment_pdf"
```

---

## Task 4: Heuristic heading fallback for outline-less PDFs

**Files:**
- Modify: `backend/app/services/pdf_import.py`
- Test: `backend/tests/test_pdf_segment_heuristic.py`

**Interfaces:**
- Consumes: `Segment`, `segment_pdf` (Task 3).
- Produces: `heuristic_segments(doc) -> list[Segment]`; `segment_pdf` now uses it for the no-outline case (still single-segment when no headings found). Consumed by Task 5 (LLM takes precedence) and Task 8.

**Context:** Iterate `page.get_text("dict")` spans; the modal (most common) span size is body text. A line whose max span size is meaningfully larger (`>= body_size * 1.25`) and short (<= 120 chars) is treated as a heading and starts a new segment.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_pdf_segment_heuristic.py`:

```python
import os
import sys

import fitz

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.pdf_import import segment_pdf


def _pdf_with_big_headings() -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Getting Started", fontsize=24)
    page.insert_text((72, 110), "Some body copy explaining things.", fontsize=11)
    page.insert_text((72, 200), "Advanced Usage", fontsize=24)
    page.insert_text((72, 238), "More body copy here.", fontsize=11)
    return doc.tobytes()


def test_heuristic_splits_on_large_headings_when_no_outline():
    segs = segment_pdf(_pdf_with_big_headings())
    titles = [s.title for s in segs]
    assert "Getting Started" in titles
    assert "Advanced Usage" in titles
    assert len(segs) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_pdf_segment_heuristic.py -v`
Expected: FAIL — only one (whole-document) segment is returned.

- [ ] **Step 3: Implement the heuristic and wire it in**

In `backend/app/services/pdf_import.py`, add (above `segment_pdf`):

```python
import collections


def _body_font_size(doc: "fitz.Document") -> float:
    sizes: collections.Counter = collections.Counter()
    for page in doc:
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    sizes[round(span["size"], 1)] += len(span.get("text", ""))
    return sizes.most_common(1)[0][0] if sizes else 12.0


def heuristic_segments(doc: "fitz.Document") -> list[Segment]:
    """Detect headings by font size (>= 1.25x body, short line) and split there.
    Returns [] when no headings stand out (caller falls back to single segment)."""
    body = _body_font_size(doc)
    threshold = body * 1.25
    headings: list[tuple[int, str]] = []  # (page0, title)
    for pno, page in enumerate(doc):
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                if not spans:
                    continue
                text = "".join(s.get("text", "") for s in spans).strip()
                max_size = max((s["size"] for s in spans), default=0)
                if text and len(text) <= 120 and max_size >= threshold:
                    headings.append((pno, text))
    if not headings:
        return []
    last_page = doc.page_count - 1
    segs: list[Segment] = []
    for i, (pno, title) in enumerate(headings):
        end = headings[i + 1][0] - 1 if i + 1 < len(headings) else last_page
        end = max(pno, end)
        segs.append(Segment(title=title, level=1, page_start=pno,
                            page_end=end, path=[title]))
    return segs
```

Then change the no-outline branch in `segment_pdf` (replace the worst-case return) to try the heuristic first:

```python
        segs = _outline_segments(doc)
        if segs:
            return segs
        segs = heuristic_segments(doc)
        if segs:
            return segs
        return [Segment(title="Document", level=1, page_start=0,
                        page_end=max(0, doc.page_count - 1), path=[])]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_pdf_segment_heuristic.py tests/test_pdf_segment.py -v`
Expected: PASS (both the new heuristic test and the Task 3 tests — the no-outline single-line PDF still yields one segment because its lone line isn't large enough to be a heading).

- [ ] **Step 5: Commit**

```bash
git add app/services/pdf_import.py tests/test_pdf_segment_heuristic.py
git commit -m "feat(pdf): heuristic heading fallback for outline-less PDFs"
```

---

## Task 5: LLM segmentation fallback

**Files:**
- Modify: `backend/app/services/pdf_import.py`
- Test: `backend/tests/test_pdf_segment_llm.py`

**Interfaces:**
- Consumes: `Segment`, `heuristic_segments`, `segment_pdf` (Tasks 3-4).
- Produces: `async def segment_pdf_async(pdf_bytes: bytes) -> list[Segment]` — outline → (LLM when `llm_fallback_enabled`) → heuristic → single. `_llm_segment_titles(text: str) -> list[dict]` (the awaitable LLM call). Consumed by Task 8 (which awaits `segment_pdf_async`).

**Context:** Reuse the existing LLM call pattern in `app/services/profiles/llm.py` (an async httpx POST to the configured provider). The LLM returns an ordered JSON list of `{"title": str, "level": int}`; each title is matched to its first occurrence in the page text to assign a page. Keep `segment_pdf` (sync, no LLM) for the unit tests in Tasks 3-4; `segment_pdf_async` is the production entry the pipeline awaits. The test stubs `_llm_segment_titles` so no network is hit.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_pdf_segment_llm.py`:

```python
import os
import sys

import fitz
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.services.pdf_import as pdf_import
from app.services.pdf_import import segment_pdf_async

pytestmark = pytest.mark.asyncio


def _pdf_plain_text() -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Overview", fontsize=11)
    page.insert_text((72, 100), "Configuration", fontsize=11)
    return doc.tobytes()


async def test_llm_fallback_used_when_enabled(monkeypatch):
    monkeypatch.setattr(pdf_import.settings, "llm_fallback_enabled", True)

    async def fake_llm(text):
        return [{"title": "Overview", "level": 1},
                {"title": "Configuration", "level": 1}]

    monkeypatch.setattr(pdf_import, "_llm_segment_titles", fake_llm)
    segs = await segment_pdf_async(_pdf_plain_text())
    assert [s.title for s in segs] == ["Overview", "Configuration"]


async def test_outline_still_wins_without_calling_llm(monkeypatch):
    monkeypatch.setattr(pdf_import.settings, "llm_fallback_enabled", True)

    async def boom(text):
        raise AssertionError("LLM must not be called when an outline exists")

    monkeypatch.setattr(pdf_import, "_llm_segment_titles", boom)
    doc = fitz.open()
    doc.new_page(); doc.new_page()
    doc.set_toc([[1, "A", 1], [1, "B", 2]])
    segs = await segment_pdf_async(doc.tobytes())
    assert [s.title for s in segs] == ["A", "B"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_pdf_segment_llm.py -v`
Expected: FAIL — `ImportError`/`AttributeError` for `segment_pdf_async` / `_llm_segment_titles`.

- [ ] **Step 3: Implement**

In `backend/app/services/pdf_import.py` add the import and functions:

```python
from app.core.config import settings
from app.services.profiles import llm as llm_mod
```

```python
def _full_text_with_pages(doc: "fitz.Document") -> list[str]:
    """Per-page plain text (index = 0-based page number)."""
    return [page.get_text("text") for page in doc]


async def _llm_segment_titles(text: str) -> list[dict]:
    """Ask the configured LLM for an ordered list of {title, level} section
    headings. Returns [] on any failure (caller falls back to heuristic)."""
    prompt = (
        "You are given the plain text of a documentation PDF. Identify its "
        "section headings in reading order. Respond with ONLY a JSON array of "
        'objects like {"title": "...", "level": 1}, where level 1 is a top '
        "section and deeper levels are subsections. No prose.\n\n"
        f"TEXT:\n{text[:24000]}"
    )
    try:
        raw = await llm_mod.call_llm(prompt)  # see note below
        import json
        data = json.loads(llm_mod._strip_fences(raw))
        out = []
        for item in data:
            t = str(item.get("title", "")).strip()
            if t:
                out.append({"title": t, "level": int(item.get("level", 1) or 1)})
        return out
    except Exception as exc:  # noqa: BLE001 - fallback is intentional
        logger.warning("LLM segmentation failed, falling back: %s", exc)
        return []


def _titles_to_segments(doc: "fitz.Document", titles: list[dict]) -> list[Segment]:
    pages = _full_text_with_pages(doc)
    last_page = doc.page_count - 1
    located: list[tuple[int, str, int]] = []  # (page0, title, level)
    for item in titles:
        title = item["title"]
        page0 = next((i for i, t in enumerate(pages) if title in t), None)
        if page0 is not None:
            located.append((page0, title, item["level"]))
    if not located:
        return []
    segs: list[Segment] = []
    stack: list[str] = []
    for i, (pno, title, level) in enumerate(located):
        end = located[i + 1][0] - 1 if i + 1 < len(located) else last_page
        end = max(pno, end)
        stack = stack[: level - 1]
        stack.append(title)
        segs.append(Segment(title=title, level=level, page_start=pno,
                            page_end=end, path=list(stack)))
    return segs


async def segment_pdf_async(pdf_bytes: bytes) -> list[Segment]:
    """Production segmenter: outline-first, then LLM (when enabled), then the
    font-size heuristic, then a single whole-document segment."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        segs = _outline_segments(doc)
        if segs:
            return segs
        if settings.llm_fallback_enabled:
            text = "\n".join(_full_text_with_pages(doc))
            titles = await _llm_segment_titles(text)
            segs = _titles_to_segments(doc, titles)
            if segs:
                return segs
        segs = heuristic_segments(doc)
        if segs:
            return segs
        return [Segment(title="Document", level=1, page_start=0,
                        page_end=max(0, doc.page_count - 1), path=[])]
    finally:
        doc.close()
```

> Note on `llm_mod.call_llm`: inspect `app/services/profiles/llm.py` for the existing provider-dispatch call used by `derive_spec` (it builds the request body for anthropic/openai from `settings.llm_*` and returns the model's text). If a reusable single-call helper exists, call it; if the call logic is inline inside `derive_spec`, extract the minimal request/response part into a module-level `async def call_llm(prompt: str) -> str` in `llm.py` and use it from both places (DRY). Keep `_strip_fences` usage to tolerate fenced JSON.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_pdf_segment_llm.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/pdf_import.py app/services/profiles/llm.py tests/test_pdf_segment_llm.py
git commit -m "feat(pdf): LLM segmentation fallback for outline-less PDFs"
```

---

## Task 6: `acquire_pdf` + `pdf_is_upload`

**Files:**
- Modify: `backend/app/services/pdf_import.py`
- Test: `backend/tests/test_pdf_acquire.py`

**Interfaces:**
- Produces: `pdf_is_upload(source) -> bool`; `async def acquire_pdf(source) -> tuple[bytes, str]` (bytes + sha256 hex); `class PdfAcquireError(Exception)`; `pdf_path_for(source_id, pdf_dir) -> str`. Consumed by Tasks 8, 10.

**Context:** `pdf_is_upload` checks `base_url.startswith("file://")`. Upload origin reads `<settings.pdf_dir>/<source_id>.pdf`. URL origin downloads `base_url` via a module-level `async def _fetch_url_bytes(url) -> bytes` (httpx, 30s connect / 300s read) — the test monkeypatches that function so no network is used.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_pdf_acquire.py`:

```python
import hashlib
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.services.pdf_import as pdf_import
from app.services.pdf_import import acquire_pdf, pdf_is_upload, pdf_path_for

pytestmark = pytest.mark.asyncio


def _src(base_url):
    import uuid
    return types.SimpleNamespace(id=uuid.uuid4(), base_url=base_url)


async def test_upload_origin_reads_file_and_hashes(tmp_path, monkeypatch):
    monkeypatch.setattr(pdf_import.settings, "pdf_dir", str(tmp_path))
    src = _src("file://x.pdf")
    data = b"%PDF-1.4 fake bytes"
    with open(pdf_path_for(src.id, str(tmp_path)), "wb") as fh:
        fh.write(data)
    blob, digest = await acquire_pdf(src)
    assert blob == data
    assert digest == hashlib.sha256(data).hexdigest()
    assert pdf_is_upload(src) is True


async def test_url_origin_downloads_and_hashes(monkeypatch):
    src = _src("https://example.com/doc.pdf")
    data = b"%PDF-1.4 url bytes"

    async def fake_fetch(url):
        assert url == src.base_url
        return data

    monkeypatch.setattr(pdf_import, "_fetch_url_bytes", fake_fetch)
    blob, digest = await acquire_pdf(src)
    assert blob == data
    assert digest == hashlib.sha256(data).hexdigest()
    assert pdf_is_upload(src) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_pdf_acquire.py -v`
Expected: FAIL — names not importable.

- [ ] **Step 3: Implement**

In `backend/app/services/pdf_import.py` add:

```python
import hashlib
import os

import httpx


class PdfAcquireError(Exception):
    """Raised when a PDF source's bytes cannot be obtained."""


def pdf_is_upload(source) -> bool:
    return str(source.base_url).startswith("file://")


def pdf_path_for(source_id, pdf_dir: str) -> str:
    return os.path.join(pdf_dir, f"{source_id}.pdf")


async def _fetch_url_bytes(url: str) -> bytes:
    timeout = httpx.Timeout(300.0, connect=30.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.content


async def acquire_pdf(source) -> tuple[bytes, str]:
    """Return (pdf_bytes, sha256_hex) for a pdf source (upload or URL origin)."""
    try:
        if pdf_is_upload(source):
            path = pdf_path_for(source.id, settings.pdf_dir)
            with open(path, "rb") as fh:
                data = fh.read()
        else:
            data = await _fetch_url_bytes(source.base_url)
    except (OSError, httpx.HTTPError) as exc:
        raise PdfAcquireError(f"Could not acquire PDF: {exc}") from exc
    if not data:
        raise PdfAcquireError("PDF is empty")
    return data, hashlib.sha256(data).hexdigest()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_pdf_acquire.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/pdf_import.py tests/test_pdf_acquire.py
git commit -m "feat(pdf): acquire_pdf (url/upload) + hashing"
```

---

## Task 7: `segment_to_markdown`

**Files:**
- Modify: `backend/app/services/pdf_import.py`
- Test: `backend/tests/test_pdf_to_markdown.py`

**Interfaces:**
- Consumes: `Segment` (Task 3), `sanitize_markdown` from `app.services.sanitize`.
- Produces: `segment_to_markdown(pdf_bytes: bytes, segment: Segment) -> str`. Consumed by Task 8.

**Context:** `pymupdf4llm.to_markdown(doc, pages=[...])` converts specific pages to markdown. Open the doc once per call from bytes, render the segment's page range, run `sanitize_markdown` to strip recurring chrome (the web path applies the same).

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_pdf_to_markdown.py`:

```python
import os
import sys

import fitz

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.pdf_import import segment_to_markdown, Segment


def _pdf() -> bytes:
    doc = fitz.open()
    p0 = doc.new_page(); p0.insert_text((72, 72), "Alpha section content")
    p1 = doc.new_page(); p1.insert_text((72, 72), "Beta section content")
    return doc.tobytes()


def test_renders_only_segment_pages():
    pdf = _pdf()
    md = segment_to_markdown(pdf, Segment("Alpha", 1, 0, 0, ["Alpha"]))
    assert "Alpha section content" in md
    assert "Beta section content" not in md
    assert md.strip()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_pdf_to_markdown.py -v`
Expected: FAIL — `ImportError: cannot import name 'segment_to_markdown'`.

- [ ] **Step 3: Implement**

In `backend/app/services/pdf_import.py` add:

```python
import pymupdf4llm

from app.services.sanitize import sanitize_markdown
```

```python
def segment_to_markdown(pdf_bytes: bytes, segment: Segment) -> str:
    """Render a segment's page range to clean markdown."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        pages = list(range(segment.page_start, segment.page_end + 1))
        md = pymupdf4llm.to_markdown(doc, pages=pages)
    finally:
        doc.close()
    return sanitize_markdown(md or "")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_pdf_to_markdown.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/pdf_import.py tests/test_pdf_to_markdown.py
git commit -m "feat(pdf): segment_to_markdown via pymupdf4llm"
```

---

## Task 8: `run_pdf_extraction` orchestration

**Files:**
- Modify: `backend/app/services/pdf_import.py`
- Test: `backend/tests/test_pdf_run_extraction.py`

**Interfaces:**
- Consumes: `acquire_pdf`, `segment_pdf_async`, `segment_to_markdown` (Tasks 5-7), `derive_pdf_topic_key` (Task 2), and the `FirecrawlService` instance's `process_article_result` + `_reconcile_removals`.
- Produces: `async def run_pdf_extraction(service, db, source, run, run_pk) -> ExtractionRun`. Consumed by Task 9.

**Context (mirror the web completion path):** build & persist the TOC (delete-and-rebuild), call `service.process_article_result(...)` per segment, reload the run by PK, `_reconcile_removals`, re-read counters, set status/`pdf_hash`/`source.last_extracted_at`. The fast path: if the source's most recent COMPLETED run has the same `pdf_hash` and articles exist, bump `extracted_at` on all and mark unchanged without re-segmenting.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_pdf_run_extraction.py`:

```python
import os
import sys
import uuid

import fitz
import pytest
import pytest_asyncio
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import (
    AsyncSession, async_sessionmaker, create_async_engine,
)
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core.database import Base
from app.models import (
    Vendor, Product, DocumentationSource, ExtractionRun, Article,
)
from app.models.article_version import ArticleVersion
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


def _pdf(extra="") -> bytes:
    doc = fitz.open()
    for i in range(2):
        page = doc.new_page()
        page.insert_text((72, 72), f"Body for chapter {i+1}. {extra}")
    doc.set_toc([[1, "Chapter 1", 1], [1, "Chapter 2", 2]])
    return doc.tobytes()


async def _make_pdf_source(factory, tmp_path) -> uuid.UUID:
    settings.pdf_dir = str(tmp_path)
    async with factory() as s:
        v = Vendor(name="V"); s.add(v); await s.flush()
        p = Product(vendor_id=v.id, name="P"); s.add(p); await s.flush()
        src = DocumentationSource(
            product_id=p.id, name="Manual",
            base_url="file://x.pdf", source_type="pdf",
        )
        s.add(src); await s.commit()
        sid = src.id
    with open(pdf_path_for(sid, str(tmp_path)), "wb") as fh:
        fh.write(_pdf())
    return sid


async def _run(factory, sid) -> uuid.UUID:
    svc = FirecrawlService()
    async with factory() as s:
        src = await s.get(DocumentationSource, sid)
        run = ExtractionRun(source_id=sid)
        s.add(run); await s.flush()
        run_pk = run.id
        await run_pdf_extraction(svc, s, src, run, run_pk)
        await s.commit()
    return run_pk


async def test_first_run_creates_articles(factory, tmp_path):
    sid = await _make_pdf_source(factory, tmp_path)
    await _run(factory, sid)
    async with factory() as s:
        arts = (await s.execute(
            select(Article).where(Article.source_id == sid).order_by(Article.sort_order)
        )).scalars().all()
        assert [a.title for a in arts] == ["Chapter 1", "Chapter 2"]
        assert all(a.content_markdown.strip() for a in arts)


async def test_second_identical_run_is_all_unchanged(factory, tmp_path):
    sid = await _make_pdf_source(factory, tmp_path)
    await _run(factory, sid)
    run2 = await _run(factory, sid)
    async with factory() as s:
        r = await s.get(ExtractionRun, run2)
        assert r.articles_unchanged == 2
        assert r.articles_extracted == 0
        assert r.pdf_hash is not None


async def test_modified_pdf_diffs(factory, tmp_path):
    sid = await _make_pdf_source(factory, tmp_path)
    await _run(factory, sid)
    # Replace the stored file with modified content, then re-run.
    with open(pdf_path_for(sid, str(tmp_path)), "wb") as fh:
        fh.write(_pdf(extra="CHANGED"))
    await _run(factory, sid)
    async with factory() as s:
        nver = (await s.execute(
            select(func.count()).select_from(ArticleVersion)
            .join(Article, Article.id == ArticleVersion.article_id)
            .where(Article.source_id == sid)
        )).scalar()
        assert nver >= 1  # at least one prior version snapshotted
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_pdf_run_extraction.py -v`
Expected: FAIL — `ImportError: cannot import name 'run_pdf_extraction'`.

- [ ] **Step 3: Implement**

In `backend/app/services/pdf_import.py` add the imports and orchestration:

```python
import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, func, select, update

from app.models.article import Article
from app.models.extraction_run import ExtractionRun, RunStatus
from app.models.source import DocumentationSource, SourceStatus
from app.models.toc import TOCEntry
from app.services.versioning import derive_pdf_topic_key
```

```python
async def _latest_completed_hash(db, source_id) -> str | None:
    return (
        await db.execute(
            select(ExtractionRun.pdf_hash)
            .where(
                ExtractionRun.source_id == source_id,
                ExtractionRun.status == RunStatus.COMPLETED,
                ExtractionRun.pdf_hash.isnot(None),
            )
            .order_by(ExtractionRun.started_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def run_pdf_extraction(service, db, source, run, run_pk) -> ExtractionRun:
    """Extract a PDF source into Article rows, reusing the web path's diff/version
    machinery. `service` is a FirecrawlService (for process_article_result /
    _reconcile_removals)."""
    run.current_phase = "pdf_acquire"
    source.status = SourceStatus.EXTRACTING
    await db.commit()

    pdf_bytes, pdf_hash = await acquire_pdf(source)

    # Fast path: byte-identical to the last completed run → mark all unchanged.
    prior = await _latest_completed_hash(db, source.id)
    existing_count = (
        await db.execute(
            select(func.count()).select_from(Article).where(
                Article.source_id == source.id, Article.removed_at.is_(None)
            )
        )
    ).scalar()
    now = datetime.now(timezone.utc)
    if prior == pdf_hash and existing_count:
        await db.execute(
            update(Article)
            .where(Article.source_id == source.id, Article.removed_at.is_(None))
            .values(extracted_at=now)
        )
        run = (await db.execute(
            select(ExtractionRun).where(ExtractionRun.id == run_pk)
        )).scalar_one()
        run.status = RunStatus.COMPLETED
        run.completed_at = now
        run.pdf_hash = pdf_hash
        run.articles_total = existing_count
        run.articles_unchanged = existing_count
        source.status = SourceStatus.COMPLETED
        source.last_extracted_at = now
        await db.flush()
        return run

    # Segment + build the TOC tree (delete-and-rebuild, like the web path).
    segments = await segment_pdf_async(pdf_bytes)
    run.current_phase = "pdf_convert"
    run.articles_total = len(segments)
    await db.commit()

    await db.execute(delete(TOCEntry).where(TOCEntry.source_id == source.id))
    await db.flush()

    # parent via a level stack: each segment's parent is the nearest preceding
    # entry with a strictly smaller level.
    entry_ids: list[uuid.UUID] = []
    levels: list[int] = []
    article_inputs: list[tuple] = []  # (toc_id, sort_order, title, topic_key, url, md)
    for i, seg in enumerate(segments):
        parent_id = None
        for j in range(i - 1, -1, -1):
            if levels[j] < seg.level:
                parent_id = entry_ids[j]
                break
        topic_key = derive_pdf_topic_key(seg.path or [seg.title])
        page_anchor = f"#page={seg.page_start + 1}"
        url = f"{source.base_url}{page_anchor}"
        toc = TOCEntry(
            source_id=source.id, title=seg.title, url=url,
            level=seg.level, sort_order=i, is_article=True, parent_id=parent_id,
        )
        db.add(toc)
        await db.flush()
        entry_ids.append(toc.id)
        levels.append(seg.level)
        md = segment_to_markdown(pdf_bytes, seg)
        article_inputs.append((toc.id, i, seg.title, topic_key, url, md))

    run.current_phase = "content_scraping"
    await db.commit()

    for toc_id, sort_order, title, topic_key, url, md in article_inputs:
        await service.process_article_result(
            db, source.id, run_pk, url=url, markdown_content=md, doc_html="",
            toc_entry_id=toc_id, sort_order=sort_order, title=title,
            change_status=None, topic_key=topic_key,
        )

    run = (await db.execute(
        select(ExtractionRun).where(ExtractionRun.id == run_pk)
    )).scalar_one()
    await service._reconcile_removals(db, source.id, run_pk)

    run.status = RunStatus.COMPLETED
    run.completed_at = datetime.now(timezone.utc)
    run.pdf_hash = pdf_hash
    source.status = SourceStatus.COMPLETED
    source.last_extracted_at = run.completed_at
    await db.flush()
    return run
```

> Write the orchestration as one clean function. The fast-path `existing_count`
> query and the main path must not double-run; structure it as: acquire → fast-path
> early-return when `prior == pdf_hash and existing_count` → otherwise segment/persist.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_pdf_run_extraction.py -v`
Expected: PASS (all three tests).

- [ ] **Step 5: Commit**

```bash
git add app/services/pdf_import.py tests/test_pdf_run_extraction.py
git commit -m "feat(pdf): run_pdf_extraction orchestration with fast-path + diffing"
```

---

## Task 9: Branch `extract_source` on `source_type`

**Files:**
- Modify: `backend/app/services/firecrawl.py` (top of `extract_source`, ~line 1296 after `run_pk = run.id`)
- Test: `backend/tests/test_pdf_extract_source_branch.py`

**Interfaces:**
- Consumes: `run_pdf_extraction` (Task 8).
- Produces: `extract_source` runs the PDF pipeline for `source.source_type == "pdf"`.

**Context:** After the run/source are loaded and `run_pk = run.id` is set (and before `self._check_available()` / web TOC discovery), insert the branch. Import `pdf_import` lazily inside the function to avoid a heavy import (PyMuPDF) at module load.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_pdf_extract_source_branch.py`:

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
from app.models.extraction_run import RunStatus
from app.services.firecrawl import FirecrawlService
from app.services.pdf_import import pdf_path_for

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


async def test_extract_source_runs_pdf_pipeline(factory, tmp_path):
    settings.pdf_dir = str(tmp_path)
    async with factory() as s:
        v = Vendor(name="V"); s.add(v); await s.flush()
        p = Product(vendor_id=v.id, name="P"); s.add(p); await s.flush()
        src = DocumentationSource(product_id=p.id, name="M",
                                  base_url="file://x.pdf", source_type="pdf")
        s.add(src); await s.flush()
        run = ExtractionRun(source_id=src.id); s.add(run); await s.commit()
        sid, rid = src.id, run.id
    doc = fitz.open(); doc.new_page().insert_text((72, 72), "Hello")
    doc.set_toc([[1, "Intro", 1]])
    with open(pdf_path_for(sid, str(tmp_path)), "wb") as fh:
        fh.write(doc.tobytes())

    svc = FirecrawlService()
    async with factory() as s:
        await svc.extract_source(s, sid, run_id=rid)

    async with factory() as s:
        run = await s.get(ExtractionRun, rid)
        assert run.status == RunStatus.COMPLETED
        n = (await s.execute(select(Article).where(Article.source_id == sid))).scalars().all()
        assert len(n) == 1 and n[0].title == "Intro"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_pdf_extract_source_branch.py -v`
Expected: FAIL — the web path runs (tries `_check_available`/Firecrawl) and no articles are created (or it errors), so the assertions fail.

- [ ] **Step 3: Implement the branch**

In `backend/app/services/firecrawl.py`, immediately after `run_pk = run.id` and before the `try:`/`await self._check_available()` web flow, add:

```python
        if source.source_type == "pdf":
            from app.services import pdf_import
            try:
                return await pdf_import.run_pdf_extraction(self, db, source, run, run_pk)
            except pdf_import.PdfAcquireError as exc:
                run = (await db.execute(
                    select(ExtractionRun).where(ExtractionRun.id == run_pk)
                )).scalar_one()
                run.status = RunStatus.FAILED
                run.error_message = str(exc)[:4096]
                run.completed_at = datetime.now(timezone.utc)
                source.status = SourceStatus.FAILED
                source.last_extracted_at = run.completed_at
                await db.flush()
                return run
```

> Place this branch inside `extract_source` after the run row is RUNNING and `run_pk` is captured, but before the web-only `try` body. Confirm `select` and `datetime`/`timezone` are already imported at the top of `firecrawl.py` (they are used throughout) — no new imports needed besides the lazy `pdf_import`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_pdf_extract_source_branch.py -v`
Expected: PASS

- [ ] **Step 5: Run the full backend suite (no web-path regressions)**

Run: `python3 -m pytest -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add app/services/firecrawl.py tests/test_pdf_extract_source_branch.py
git commit -m "feat(pdf): branch extract_source into the PDF pipeline"
```

---

## Task 10: API — create/replace PDF sources

**Files:**
- Modify: `backend/app/schemas/source.py` (add `source_type` to `SourceResponse`)
- Modify: `backend/app/routes/sources.py`
- Test: `backend/tests/test_pdf_source_api.py`

**Interfaces:**
- Consumes: `settings.pdf_dir`, `settings.pdf_max_upload_bytes`, `pdf_path_for` (Task 6).
- Produces: `POST /api/sources/pdf` (multipart), `PUT /api/sources/{id}/pdf` (multipart), `SourceResponse.source_type`.

**Context:** FastAPI multipart uses `Form(...)` + `UploadFile = File(None)`. The endpoint accepts `product_id`, `name`, optional `pdf_url`, optional `file`. Exactly one of `pdf_url`/`file` must be supplied. Save uploads to `<settings.pdf_dir>/<source_id>.pdf` (create the dir). Reject non-`application/pdf` (415) and oversize (413).

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_pdf_source_api.py`:

```python
import io
import os
import sys
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncSession, async_sessionmaker, create_async_engine,
)
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core.database import Base, get_db
from app.main import app
from app.models import Vendor, Product, DocumentationSource

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def client(tmp_path):
    settings.pdf_dir = str(tmp_path)
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    async def override_get_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c, factory
    app.dependency_overrides.clear()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def _product(factory) -> uuid.UUID:
    async with factory() as s:
        v = Vendor(name="V"); s.add(v); await s.flush()
        p = Product(vendor_id=v.id, name="P"); s.add(p); await s.commit()
        return p.id


async def test_create_pdf_source_from_url(client):
    c, factory = client
    pid = await _product(factory)
    resp = await c.post("/api/sources/pdf", data={
        "product_id": str(pid), "name": "Spec", "pdf_url": "https://x/doc.pdf",
    })
    assert resp.status_code == 201
    body = resp.json()
    assert body["source_type"] == "pdf"
    assert body["base_url"] == "https://x/doc.pdf"


async def test_upload_pdf_stores_file_and_sets_marker(client, tmp_path):
    c, factory = client
    pid = await _product(factory)
    files = {"file": ("d.pdf", io.BytesIO(b"%PDF-1.4 hi"), "application/pdf")}
    resp = await c.post("/api/sources/pdf",
                        data={"product_id": str(pid), "name": "Up"}, files=files)
    assert resp.status_code == 201
    body = resp.json()
    sid = body["id"]
    assert body["base_url"] == f"file://{sid}.pdf"
    assert os.path.exists(os.path.join(str(tmp_path), f"{sid}.pdf"))


async def test_non_pdf_upload_is_415(client):
    c, factory = client
    pid = await _product(factory)
    files = {"file": ("d.txt", io.BytesIO(b"hi"), "text/plain")}
    resp = await c.post("/api/sources/pdf",
                        data={"product_id": str(pid), "name": "Bad"}, files=files)
    assert resp.status_code == 415


async def test_oversize_upload_is_413(client):
    c, factory = client
    settings.pdf_max_upload_bytes = 10
    pid = await _product(factory)
    files = {"file": ("d.pdf", io.BytesIO(b"%PDF-1.4 " + b"x" * 100), "application/pdf")}
    resp = await c.post("/api/sources/pdf",
                        data={"product_id": str(pid), "name": "Big"}, files=files)
    assert resp.status_code == 413
    settings.pdf_max_upload_bytes = 100 * 1024 * 1024
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_pdf_source_api.py -v`
Expected: FAIL — 404/405 (route missing) and `source_type` absent from responses.

- [ ] **Step 3: Add `source_type` to `SourceResponse`**

In `backend/app/schemas/source.py`, add to `SourceResponse` (after `product_id`/`job_id`):

```python
    source_type: str
```

- [ ] **Step 4: Add the routes**

In `backend/app/routes/sources.py`, add imports at the top:

```python
import os
from fastapi import File, Form, UploadFile
from app.core.config import settings
from app.services.pdf_import import pdf_path_for
```

Add this route (place it before `@router.get("/{source_id}")`, alongside `/pickable` and `/import`):

```python
@router.post("/pdf", response_model=SourceResponse, status_code=201)
async def create_pdf_source(
    product_id: uuid.UUID = Form(...),
    name: str = Form(...),
    pdf_url: str | None = Form(None),
    file: UploadFile | None = File(None),
    db: AsyncSession = Depends(get_db),
):
    """Create a PDF source from either a URL (re-fetchable) or an uploaded file."""
    product = (
        await db.execute(select(Product).where(Product.id == product_id))
    ).scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    if bool(pdf_url) == bool(file):
        raise HTTPException(status_code=422, detail="Provide exactly one of pdf_url or file")

    if pdf_url:
        source = DocumentationSource(
            product_id=product_id, name=name, base_url=pdf_url, source_type="pdf",
        )
        db.add(source)
        await db.commit()
        await db.refresh(source)
        return source

    # Upload path.
    if file.content_type not in ("application/pdf", "application/x-pdf"):
        raise HTTPException(status_code=415, detail="File must be a PDF")
    data = await file.read()
    if len(data) > settings.pdf_max_upload_bytes:
        raise HTTPException(status_code=413, detail="PDF exceeds the maximum upload size")

    source = DocumentationSource(
        product_id=product_id, name=name, base_url="pending", source_type="pdf",
    )
    db.add(source)
    await db.flush()
    source.base_url = f"file://{source.id}.pdf"
    os.makedirs(settings.pdf_dir, exist_ok=True)
    with open(pdf_path_for(source.id, settings.pdf_dir), "wb") as fh:
        fh.write(data)
    await db.commit()
    await db.refresh(source)
    return source


@router.put("/{source_id}/pdf", response_model=SourceResponse)
async def replace_pdf_file(
    source_id: uuid.UUID,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    """Replace the stored file for an upload-origin PDF source."""
    source = (
        await db.execute(select(DocumentationSource).where(DocumentationSource.id == source_id))
    ).scalar_one_or_none()
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")
    if source.source_type != "pdf" or not source.base_url.startswith("file://"):
        raise HTTPException(status_code=409, detail="Not an upload-origin PDF source")
    if file.content_type not in ("application/pdf", "application/x-pdf"):
        raise HTTPException(status_code=415, detail="File must be a PDF")
    data = await file.read()
    if len(data) > settings.pdf_max_upload_bytes:
        raise HTTPException(status_code=413, detail="PDF exceeds the maximum upload size")
    os.makedirs(settings.pdf_dir, exist_ok=True)
    with open(pdf_path_for(source.id, settings.pdf_dir), "wb") as fh:
        fh.write(data)
    await db.commit()
    await db.refresh(source)
    return source
```

> `python-multipart` is required by FastAPI for `Form`/`File`. If `python-multipart` is not already installed (import error at app startup), `python3 -m pip install python-multipart` and add `python-multipart==0.0.18` to `requirements.txt` in this task's commit.

- [ ] **Step 5: Run test to verify it passes**

Run: `python3 -m pytest tests/test_pdf_source_api.py -v`
Expected: PASS (all four tests).

- [ ] **Step 6: Commit**

```bash
git add app/schemas/source.py app/routes/sources.py tests/test_pdf_source_api.py requirements.txt
git commit -m "feat(pdf): create/replace PDF source API endpoints"
```

---

## Task 11: Frontend — move CSV import to the Vendors view

**Files:**
- Modify: `frontend/src/components/Dashboard.tsx` (remove import button/modal)
- Modify: `frontend/src/components/VendorList.tsx` (add import button/modal)
- Test: `cd frontend && npm run build && npm run lint`

**Interfaces:**
- Consumes: existing `BulkImport` component (unchanged).

- [ ] **Step 1: Remove the import UI from Dashboard.tsx**

In `frontend/src/components/Dashboard.tsx`: delete the `import BulkImport from "./BulkImport";` line, the `const [showImport, setShowImport] = useState(false);` line, the **Import CSV** button in the header, and the `{showImport && (<BulkImport .../>)}` block. Restore the header to a plain `<h2>Dashboard</h2>` (remove the `dashboard-header` wrapper if it now only holds the title).

- [ ] **Step 2: Add the import UI to VendorList.tsx**

In `frontend/src/components/VendorList.tsx`:

a) Add imports:

```typescript
import { useState } from "react";
import BulkImport from "./BulkImport";
```
(`useState` is already imported — keep one import; just add `BulkImport`.)

b) Add state in the component:

```typescript
  const [showImport, setShowImport] = useState(false);
```

c) Replace the `<h2>Vendors</h2>` header with a header row containing an Import button:

```tsx
      <div className="dashboard-header">
        <h2>Vendors</h2>
        <button className="btn-primary-sm" onClick={() => setShowImport(true)}>
          Import CSV
        </button>
      </div>
```

d) Render the modal near the end of the returned JSX (before the closing wrapper), wiring success to a vendor reload:

```tsx
      {showImport && (
        <BulkImport
          onClose={() => setShowImport(false)}
          onImported={() => {
            setShowImport(false);
            listVendors()
              .then((data) => setVendors(data.vendors))
              .catch(() => setError("Failed to load vendors"));
          }}
        />
      )}
```

- [ ] **Step 3: Build + lint**

Run: `cd frontend && npm run build && npm run lint`
Expected: build succeeds; lint reports **0 problems**.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/Dashboard.tsx frontend/src/components/VendorList.tsx
git commit -m "feat(ui): move CSV import from Dashboard to Vendors view"
```

---

## Task 12: Frontend — add PDF sources in the SourceList view

**Files:**
- Modify: `frontend/src/types/index.ts` (`source_type` on `DocumentationSource`)
- Modify: `frontend/src/api/client.ts` (PDF source calls)
- Modify: `frontend/src/components/SourceList.tsx` (type selector, badge, re-upload)
- Test: `cd frontend && npm run build && npm run lint`

**Interfaces:**
- Consumes: `POST /api/sources/pdf`, `PUT /api/sources/{id}/pdf` (Task 10).

- [ ] **Step 1: Add `source_type` to the type**

In `frontend/src/types/index.ts`, add to the `DocumentationSource` interface:

```typescript
  source_type: string;
```

- [ ] **Step 2: Add client functions**

In `frontend/src/api/client.ts`, add:

```typescript
export async function createPdfSourceFromUrl(
  productId: string,
  name: string,
  pdfUrl: string,
): Promise<DocumentationSource> {
  const form = new FormData();
  form.append("product_id", productId);
  form.append("name", name);
  form.append("pdf_url", pdfUrl);
  const res = await api.post<DocumentationSource>("/sources/pdf", form);
  return res.data;
}

export async function uploadPdfSource(
  productId: string,
  name: string,
  file: File,
): Promise<DocumentationSource> {
  const form = new FormData();
  form.append("product_id", productId);
  form.append("name", name);
  form.append("file", file);
  const res = await api.post<DocumentationSource>("/sources/pdf", form);
  return res.data;
}

export async function replacePdfFile(
  sourceId: string,
  file: File,
): Promise<DocumentationSource> {
  const form = new FormData();
  form.append("file", file);
  const res = await api.put<DocumentationSource>(`/sources/${sourceId}/pdf`, form);
  return res.data;
}
```

- [ ] **Step 3: Add the type selector + badge + re-upload to SourceList.tsx**

In `frontend/src/components/SourceList.tsx`:

a) Add to the imports from `../api/client`: `createPdfSourceFromUrl`, `uploadPdfSource`, `replacePdfFile`.

b) Add state near the existing add-source form state:

```typescript
  const [addKind, setAddKind] = useState<"web" | "pdf_url" | "pdf_upload">("web");
  const [pdfUrl, setPdfUrl] = useState("");
  const [pdfFile, setPdfFile] = useState<File | null>(null);
```

c) In the add-source form, add a kind selector and conditional inputs. Add this selector above the existing URL input:

```tsx
        <select value={addKind} onChange={(e) => setAddKind(e.target.value as typeof addKind)}>
          <option value="web">Web URL</option>
          <option value="pdf_url">PDF from URL</option>
          <option value="pdf_upload">PDF upload</option>
        </select>
```

Render the PDF inputs when a PDF kind is selected (place beside the existing name input; keep the existing web URL input rendered only when `addKind === "web"`):

```tsx
        {addKind === "pdf_url" && (
          <input
            type="url"
            placeholder="https://…/document.pdf"
            value={pdfUrl}
            onChange={(e) => setPdfUrl(e.target.value)}
            required
          />
        )}
        {addKind === "pdf_upload" && (
          <input
            type="file"
            accept="application/pdf"
            onChange={(e) => setPdfFile(e.target.files?.[0] ?? null)}
            required
          />
        )}
```

d) In the form's submit handler, branch on `addKind` (keep the existing web `createSource` call for `"web"`):

```typescript
      if (addKind === "web") {
        await createSource({ product_id: product.id, name: name.trim(), base_url: baseUrl.trim() });
      } else if (addKind === "pdf_url") {
        await createPdfSourceFromUrl(product.id, name.trim(), pdfUrl.trim());
      } else {
        if (!pdfFile) return;
        await uploadPdfSource(product.id, name.trim(), pdfFile);
      }
      setPdfUrl("");
      setPdfFile(null);
```

(Use the actual existing variable names for the source name / base URL inputs in this component — inspect the current add-source form and match them. The web branch must stay byte-for-byte equivalent to today's behavior.)

e) Show a `PDF` badge on PDF source rows. In the source row rendering, where the source name is shown:

```tsx
            {s.source_type === "pdf" && <span className="status-badge" style={{ backgroundColor: "#5a7fa3" }}>PDF</span>}
```

f) Offer re-upload for upload-origin PDFs. In the row actions, add:

```tsx
            {s.source_type === "pdf" && s.base_url.startsWith("file://") && (
              <label className="link-btn" style={{ cursor: "pointer" }}>
                Replace file
                <input
                  type="file"
                  accept="application/pdf"
                  style={{ display: "none" }}
                  onChange={(e) => {
                    const f = e.target.files?.[0];
                    if (f) replacePdfFile(s.id, f).then(() => fetchSources());
                  }}
                />
              </label>
            )}
```

- [ ] **Step 4: Build + lint**

Run: `cd frontend && npm run build && npm run lint`
Expected: build succeeds; lint **0 problems**. If any inlined handler trips a lint rule, adjust minimally (e.g. extract to a named handler) to keep 0.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/types/index.ts frontend/src/api/client.ts frontend/src/components/SourceList.tsx
git commit -m "feat(ui): add PDF sources (URL/upload) in the source list"
```

---

## Final verification

- [ ] **Backend suite**

Run: `cd backend && python3 -m pytest -q`
Expected: all tests pass (port-forward the homelab Postgres if running locally — see project memory).

- [ ] **Frontend**

Run: `cd frontend && npm run build && npm run lint`
Expected: clean build; lint 0 problems.

- [ ] **Migration applies**

Run: `cd backend && alembic upgrade head`
Expected: `source_type` + `pdf_hash` columns added; no errors.
