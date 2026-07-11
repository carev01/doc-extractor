# VLM Image-Description Enrichment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Describe meaningful documentation images with a VLM, inject the descriptions as captions into article markdown (and expose them as structured fields), so a downstream text-RAG consumer of the delta feed can "see" screenshots and diagrams.

**Architecture:** A dedicated **enrichment phase** runs at the end of a web extraction run (after scraping/reconcile, before completion). It reads each article's already-downloaded images from disk, selects the meaningful ones (dimension/size filter over the already-boilerplate-filtered `ArticleImage` rows), describes them via an OpenAI-compatible vision endpoint (cached by image content hash, bounded by a per-run budget + circuit breaker), writes the description onto the `ArticleImage` row, injects a caption after the image in `content_markdown`, and emits an `updated` `content_changes` row so the delta feed delivers the enriched content. The extraction change-detection hot path is untouched.

**Tech Stack:** FastAPI, SQLAlchemy (async/asyncpg; sync/psycopg2 in tests), Alembic, Pydantic v2, PostgreSQL, httpx, Pillow.

## Global Constraints

- Settings use the `DOCEXTRACTOR_` prefix (pydantic-settings).
- **Every new model must be imported in `app/models/__init__.py`** (and `__all__`) before `Base.metadata.create_all` runs.
- **`content_hash` is a raw-scrape change-detection fingerprint**, already decoupled from the stored `content_markdown` (which carries served `/media` URLs). **Enrichment must NOT modify `content_hash`** — doing so would break `process_article_result`'s unchanged-page fast-path and cause phantom deltas. Enrichment changes `content_markdown` + `ArticleImage` fields + emits an outbox row only.
- **Enrichment is best-effort**: a VLM outage or any error degrades to "no descriptions", never a failed run. Never raise into extraction.
- **No `ArticleVersion` from enrichment** (captions are derived enrichment, not a source revision).
- **Heartbeat-safe**: offload Pillow/CPU work with `asyncio.to_thread`; VLM calls are async httpx. Commit per-article to bound transactions.
- **Scope: web images only.** PDF-sourced figures are out of scope (the PDF path has its own VLM escalation).
- Tests run against `docextractor_test`. Data-layer tests use sync `psycopg2`; async paths use `httpx.AsyncClient`/async sessions (see `tests/test_enhanced_search.py`, `tests/test_change_log_wiring.py`).
- New settings default OFF (`image_vlm_enabled = False`); the feature is opt-in.
- Commit trailer on every commit:
  ```
  Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01LZoiNMkURTEexS4UEY8rF4
  ```
- Reference spec: `docs/superpowers/specs/2026-07-10-vlm-image-description-design.md`.

---

## File Structure

- `app/models/image.py` — **modify** — add columns to `ArticleImage`: `description`, `kind`, `width`, `height`, `is_meaningful`, `bytes_sha256`.
- `app/models/image_description.py` — **create** — `ImageDescription` cache model (keyed by `bytes_sha256`).
- `app/models/__init__.py` — **modify** — register `ImageDescription`.
- `alembic/versions/i3c4d5e6f7a8_add_image_descriptions.py` — **create** — ALTER `article_images` + CREATE `image_descriptions`.
- `backend/requirements.txt` — **modify** — add `Pillow`.
- `tests/test_defects.py` — **modify** — table invariant 19 → 20.
- `app/core/config.py` — **modify** — add `image_vlm_*` / `image_min_*` settings.
- `app/services/image_describe.py` — **create** — `evaluate_image`, `describe_image`, `inject_caption`, and the `enrich_run_images` phase driver.
- `app/services/firecrawl.py` — **modify** — call `enrich_run_images` at the end of `extract_source`.
- `app/schemas/article.py` — **modify** — surface `description`/`kind`/`width`/`height` on `ArticleImageResponse`.
- Tests: `tests/test_image_evaluate.py`, `tests/test_image_describe_vlm.py`, `tests/test_caption_inject.py`, `tests/test_image_enrich_phase.py`, `tests/test_image_surfacing.py`.

---

## Task 1: Schema — `ArticleImage` columns, `ImageDescription` cache, migration, Pillow

**Files:**
- Modify: `app/models/image.py`
- Create: `app/models/image_description.py`
- Modify: `app/models/__init__.py`
- Create: `alembic/versions/i3c4d5e6f7a8_add_image_descriptions.py`
- Modify: `backend/requirements.txt`
- Modify: `tests/test_defects.py:56-76`

**Interfaces:**
- Produces: `ArticleImage` gains `description: str|None`, `kind: str|None`, `width: int|None`, `height: int|None`, `is_meaningful: bool|None`, `bytes_sha256: str|None`. New `ImageDescription` (table `image_descriptions`): `bytes_sha256: str` PK, `description: str`, `kind: str|None`, `model: str|None`, `created_at`.

- [ ] **Step 1: Update the table invariant test**

In `tests/test_defects.py`, add `"image_descriptions"` to the sorted list (alphabetically after `export_jobs`, before `job_runs`) and bump the count message to 20:

```python
        "export_jobs",
        "extraction_runs",
        "image_descriptions",
        "job_runs",
```
and change the trailing message to `f"Expected 20 tables, got {len(table_names)}: {table_names}"`.

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest tests/test_defects.py::test_defect1_all_tables_in_metadata -v`
Expected: FAIL — `image_descriptions` missing from metadata.

- [ ] **Step 3: Add the `ArticleImage` columns**

In `app/models/image.py`, add these mapped columns to `ArticleImage` (after `sort_order`):

```python
    # Populated by the VLM image-description enrichment phase (opt-in).
    # is_meaningful: NULL = not yet evaluated; True/False = evaluated. description
    # is set only for meaningful images that have been described.
    is_meaningful: Mapped[bool | None] = mapped_column(nullable=True)
    description: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # SHA-256 of the image bytes — the cache key into image_descriptions and the
    # cross-article/source dedup key.
    bytes_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
```

Ensure `Integer` and `String` are imported in `image.py` (add to the existing `from sqlalchemy import ...`).

- [ ] **Step 4: Create the cache model**

Create `app/models/image_description.py`:

```python
"""ImageDescription — content-hash-keyed cache of VLM image descriptions.

Shared across all articles and sources: a given image's bytes are described once,
ever. Keyed by the SHA-256 of the image bytes so an identical image reused on many
pages (or re-downloaded on a later run) reuses the cached description.
"""

from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ImageDescription(Base):
    __tablename__ = "image_descriptions"

    bytes_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Which model produced it — lets a future backfill re-describe stale-model rows.
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
```

- [ ] **Step 5: Register the model**

In `app/models/__init__.py`, add after the content_change import:

```python
from app.models.image_description import ImageDescription
```
and add `"ImageDescription"` to `__all__`.

- [ ] **Step 6: Run the invariant test to verify it passes**

Run: `pytest tests/test_defects.py::test_defect1_all_tables_in_metadata -v`
Expected: PASS.

- [ ] **Step 7: Add Pillow to requirements**

Append `Pillow` to `backend/requirements.txt` (a line `Pillow`), then install it:

Run: `cd backend && pip install Pillow && python3 -c "from PIL import Image; print('pillow ok')"`
Expected: `pillow ok`.

- [ ] **Step 8: Create the migration**

Confirm the head: `cd backend && alembic heads` → must print `h2b3c4d5e6f7 (head)`. Create `alembic/versions/i3c4d5e6f7a8_add_image_descriptions.py`:

```python
"""add image_descriptions cache + ArticleImage description columns

Revision ID: i3c4d5e6f7a8
Revises: h2b3c4d5e6f7
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "i3c4d5e6f7a8"
down_revision: Union[str, Sequence[str], None] = "h2b3c4d5e6f7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("article_images", sa.Column("is_meaningful", sa.Boolean(), nullable=True))
    op.add_column("article_images", sa.Column("description", sa.String(4096), nullable=True))
    op.add_column("article_images", sa.Column("kind", sa.String(16), nullable=True))
    op.add_column("article_images", sa.Column("width", sa.Integer(), nullable=True))
    op.add_column("article_images", sa.Column("height", sa.Integer(), nullable=True))
    op.add_column("article_images", sa.Column("bytes_sha256", sa.String(64), nullable=True))
    op.create_index("ix_article_images_bytes_sha256", "article_images", ["bytes_sha256"])

    op.create_table(
        "image_descriptions",
        sa.Column("bytes_sha256", sa.String(64), primary_key=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=True),
        sa.Column("model", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("image_descriptions")
    op.drop_index("ix_article_images_bytes_sha256", table_name="article_images")
    for col in ("bytes_sha256", "height", "width", "kind", "description", "is_meaningful"):
        op.drop_column("article_images", col)
```

- [ ] **Step 9: Verify the migration applies with a single head**

Run: `cd backend && alembic upgrade head && alembic heads`
Expected: applies without error; one head `i3c4d5e6f7a8 (head)`.

- [ ] **Step 10: Commit**

```bash
git add backend/app/models/image.py backend/app/models/image_description.py \
        backend/app/models/__init__.py backend/alembic/versions/i3c4d5e6f7a8_add_image_descriptions.py \
        backend/requirements.txt backend/tests/test_defects.py
git commit -m "feat(images): ArticleImage description columns + image_descriptions cache + migration"
```

---

## Task 2: Config + `evaluate_image` selection helper

**Files:**
- Modify: `app/core/config.py`
- Create: `app/services/image_describe.py` (first slice: settings-backed `evaluate_image`)
- Test: `tests/test_image_evaluate.py`

**Interfaces:**
- Produces: settings `image_vlm_enabled: bool`, `image_vlm_base_url: str`, `image_vlm_api_key: str`, `image_vlm_model: str`, `image_vlm_max_per_run: int`, `image_vlm_max_consecutive_failures: int`, `image_vlm_max_tokens: int`, `image_min_dimension: int`, `image_min_bytes: int`.
- Produces: `@dataclass ImageEval(is_meaningful: bool, width: int | None, height: int | None, bytes_sha256: str)`; `def evaluate_image(data: bytes) -> ImageEval` (sync; callers wrap in `asyncio.to_thread`).

- [ ] **Step 1: Add the settings**

In `app/core/config.py`, immediately after the `pdf_vlm_max_consecutive_failures` line (currently line 129), add:

```python
    # ── Image VLM description (Spec 2, opt-in) ──
    # OpenAI-compatible vision chat-completions endpoint (OpenRouter by default),
    # kept separate from pdf_vlm_* so image and PDF budgets tune independently.
    image_vlm_enabled: bool = False
    image_vlm_base_url: str = "https://openrouter.ai/api/v1/chat/completions"
    image_vlm_api_key: str = ""                 # Bearer key (env only)
    image_vlm_model: str = "qwen/qwen3-vl-32b-instruct"
    image_vlm_max_per_run: int = 100            # budget: max NEW descriptions per run
    image_vlm_max_consecutive_failures: int = 5  # circuit breaker
    image_vlm_max_tokens: int = 300
    # Selection thresholds: images smaller than either are treated as decorative.
    image_min_dimension: int = 100
    image_min_bytes: int = 3072
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_image_evaluate.py`:

```python
"""evaluate_image: dimension/size selection over raw image bytes."""
import io
import os
import sys

from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.image_describe import evaluate_image


def _png(w, h, color=(120, 130, 140)):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


def test_large_image_is_meaningful():
    data = _png(400, 300)
    ev = evaluate_image(data)
    assert ev.is_meaningful is True
    assert ev.width == 400 and ev.height == 300
    assert len(ev.bytes_sha256) == 64


def test_tiny_dimension_rejected():
    data = _png(40, 40)
    ev = evaluate_image(data)
    assert ev.is_meaningful is False


def test_sub_min_bytes_rejected():
    # A 1x1 PNG is well under image_min_bytes.
    data = _png(1, 1)
    ev = evaluate_image(data)
    assert ev.is_meaningful is False


def test_non_raster_bytes_rejected():
    # SVG / corrupt bytes: Pillow can't open → not meaningful, dims None, hash set.
    data = b"<svg xmlns='http://www.w3.org/2000/svg'></svg>" * 100
    ev = evaluate_image(data)
    assert ev.is_meaningful is False
    assert ev.width is None and ev.height is None
    assert len(ev.bytes_sha256) == 64


def test_hash_is_stable_and_content_addressed():
    a = _png(400, 300, (10, 20, 30))
    assert evaluate_image(a).bytes_sha256 == evaluate_image(a).bytes_sha256
    b = _png(400, 300, (200, 100, 50))
    assert evaluate_image(a).bytes_sha256 != evaluate_image(b).bytes_sha256
```

- [ ] **Step 3: Run to verify it fails**

Run: `pytest tests/test_image_evaluate.py -v`
Expected: FAIL — `app.services.image_describe` does not exist.

- [ ] **Step 4: Implement `evaluate_image`**

Create `app/services/image_describe.py`:

```python
"""VLM image-description enrichment.

evaluate_image  — dimension/size selection over raw bytes (sync; wrap in to_thread).
describe_image  — one OpenAI-compatible vision call → ImageDescription | None (Task 3).
inject_caption  — idempotent caption injection into markdown (Task 4).
enrich_run_images — the per-run enrichment phase (Task 5).
"""

import hashlib
import io
from dataclasses import dataclass

from app.core.config import settings


@dataclass
class ImageEval:
    is_meaningful: bool
    width: int | None
    height: int | None
    bytes_sha256: str


def evaluate_image(data: bytes) -> ImageEval:
    """Classify raw image bytes as meaningful (worth describing) or decorative.

    Boilerplate (skins/ui-icons/spacers) is already filtered at download time, so
    this only screens by size and pixel dimensions. Non-raster (e.g. SVG) or
    corrupt bytes can't be measured → treated as not meaningful."""
    sha = hashlib.sha256(data).hexdigest()
    if len(data) < settings.image_min_bytes:
        return ImageEval(False, None, None, sha)
    try:
        from PIL import Image  # local import: Pillow is only needed here
        with Image.open(io.BytesIO(data)) as img:
            w, h = img.size
    except Exception:  # noqa: BLE001 — unreadable/non-raster → not meaningful
        return ImageEval(False, None, None, sha)
    meaningful = w >= settings.image_min_dimension and h >= settings.image_min_dimension
    return ImageEval(meaningful, w, h, sha)
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `pytest tests/test_image_evaluate.py -v`
Expected: 5 passed.

- [ ] **Step 6: Commit**

```bash
git add backend/app/core/config.py backend/app/services/image_describe.py backend/tests/test_image_evaluate.py
git commit -m "feat(images): image_vlm settings + evaluate_image selection helper"
```

---

## Task 3: `describe_image` — the VLM vision call

**Files:**
- Modify: `app/services/image_describe.py`
- Test: `tests/test_image_describe_vlm.py`

**Interfaces:**
- Consumes: `settings.image_vlm_*`.
- Produces: `@dataclass ImageDescription(text: str, kind: str)`; `async def describe_image(data: bytes, alt_text: str | None, *, mime: str = "image/png", client=None) -> ImageDescription | None`. Returns `None` on missing key / HTTP error / parse failure (so the caller's circuit breaker can distinguish failure from a real description). `kind ∈ {screenshot, diagram, chart, photo, other}`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_image_describe_vlm.py`:

```python
"""describe_image: OpenAI-compatible vision call, mocked via httpx.MockTransport."""
import asyncio
import json
import os
import sys

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.services.image_describe import describe_image


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_happy_path_returns_text_and_kind(monkeypatch):
    monkeypatch.setattr(settings, "image_vlm_api_key", "k")
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps(
                {"description": "Architecture diagram of the backup proxy.", "kind": "diagram"})}}]
        })

    async def run():
        async with _client(handler) as c:
            return await describe_image(b"\x89PNGfakebytes", "topology", client=c)

    res = asyncio.run(run())
    assert res is not None
    assert res.text == "Architecture diagram of the backup proxy."
    assert res.kind == "diagram"
    # Request carries the image as a base64 data URL in a vision content part.
    content = captured["body"]["messages"][-1]["content"]
    assert any(p.get("type") == "image_url" and p["image_url"]["url"].startswith("data:image/")
               for p in content)


def test_service_error_returns_none(monkeypatch):
    monkeypatch.setattr(settings, "image_vlm_api_key", "k")

    def handler(request):
        return httpx.Response(500, text="upstream error")

    async def run():
        async with _client(handler) as c:
            return await describe_image(b"bytes", None, client=c)

    assert asyncio.run(run()) is None


def test_unknown_kind_falls_back_to_other(monkeypatch):
    monkeypatch.setattr(settings, "image_vlm_api_key", "k")

    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(
            {"description": "A thing.", "kind": "banana"})}}]})

    async def run():
        async with _client(handler) as c:
            return await describe_image(b"bytes", None, client=c)

    res = asyncio.run(run())
    assert res is not None and res.kind == "other"


def test_missing_api_key_returns_none(monkeypatch):
    monkeypatch.setattr(settings, "image_vlm_api_key", "")
    assert asyncio.run(describe_image(b"bytes", None)) is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_image_describe_vlm.py -v`
Expected: FAIL — `describe_image` / `ImageDescription` not defined.

- [ ] **Step 3: Implement `describe_image`**

Append to `app/services/image_describe.py` (add imports `import base64`, `import json`, `import logging`, `import httpx` at the top; add `logger = logging.getLogger(__name__)`):

```python
_KINDS = {"screenshot", "diagram", "chart", "photo", "other"}

_PROMPT = (
    "You are describing an image from software product documentation so it can be "
    "found by search. If it is a screenshot, state which screen or dialog it shows "
    "and the action or state depicted. If it is a diagram or chart, name the "
    "components and their relationships or the trend shown. Be concise (1-3 "
    "sentences). Ignore window chrome, browser frames, and decorative borders. "
    'Respond as JSON: {"description": "...", "kind": "screenshot|diagram|chart|photo|other"}.'
)


@dataclass
class ImageDescription:
    text: str
    kind: str


async def describe_image(
    data: bytes, alt_text: str | None, *, mime: str = "image/png", client: "httpx.AsyncClient | None" = None
) -> "ImageDescription | None":
    """Describe one image via the configured vision endpoint. Returns None on
    missing key / HTTP error / parse failure (never raises)."""
    if not settings.image_vlm_api_key:
        return None
    b64 = base64.b64encode(data).decode("ascii")
    text_part = _PROMPT if not alt_text else f"{_PROMPT}\n\nAuthor's alt text: {alt_text}"
    body = {
        "model": settings.image_vlm_model,
        "max_tokens": settings.image_vlm_max_tokens,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": text_part},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ],
        }],
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {settings.image_vlm_api_key}", "content-type": "application/json"}
    own = client is None
    c = client or httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))
    try:
        resp = await c.post(settings.image_vlm_base_url, headers=headers, json=body)
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        obj = json.loads(content)
        text = (obj.get("description") or "").strip()
        if not text:
            return None
        kind = obj.get("kind")
        return ImageDescription(text=text, kind=kind if kind in _KINDS else "other")
    except Exception as exc:  # noqa: BLE001
        logger.warning("describe_image failed: %s", exc)
        return None
    finally:
        if own:
            await c.aclose()
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/test_image_describe_vlm.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/image_describe.py backend/tests/test_image_describe_vlm.py
git commit -m "feat(images): describe_image VLM vision call"
```

---

## Task 4: `inject_caption` — idempotent markdown caption

**Files:**
- Modify: `app/services/image_describe.py`
- Test: `tests/test_caption_inject.py`

**Interfaces:**
- Produces: `def inject_caption(markdown: str, image_url: str, description: str) -> str`. Inserts a `> **Figure:** …` blockquote immediately after the first markdown image reference to `image_url`; if such a caption block already follows that image, it is replaced (idempotent). If `image_url` is not found, returns `markdown` unchanged.

- [ ] **Step 1: Write the failing test**

Create `tests/test_caption_inject.py`:

```python
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.image_describe import inject_caption

URL = "/media/abc/x.png"


def test_inserts_caption_after_image():
    md = f"# Title\n\n![topology]({URL})\n\nBody text."
    out = inject_caption(md, URL, "A topology diagram.")
    assert f"![topology]({URL})" in out
    assert "> **Figure:** A topology diagram." in out
    # Caption sits after the image, before the body.
    assert out.index("> **Figure:**") > out.index(URL)
    assert out.index("> **Figure:**") < out.index("Body text.")


def test_idempotent_same_description():
    md = f"![t]({URL})\n\nBody."
    once = inject_caption(md, URL, "Desc one.")
    twice = inject_caption(once, URL, "Desc one.")
    assert once == twice


def test_replaces_existing_caption():
    md = f"![t]({URL})\n\nBody."
    first = inject_caption(md, URL, "Old description.")
    second = inject_caption(first, URL, "New description.")
    assert "New description." in second
    assert "Old description." not in second
    assert second.count("> **Figure:**") == 1


def test_missing_url_unchanged():
    md = "No image here.\n"
    assert inject_caption(md, URL, "Desc.") == md
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_caption_inject.py -v`
Expected: FAIL — `inject_caption` not defined.

- [ ] **Step 3: Implement `inject_caption`**

Append to `app/services/image_describe.py` (add `import re` at the top):

```python
def _caption_block(description: str) -> str:
    # Single-line blockquote caption (description is already 1-3 sentences).
    one_line = " ".join(description.split())
    return f"> **Figure:** {one_line}"


def inject_caption(markdown: str, image_url: str, description: str) -> str:
    """Insert (or replace) a '> **Figure:** …' caption immediately after the first
    markdown image reference to *image_url*. Idempotent for a given (image, text)."""
    lines = markdown.split("\n")
    # Find the line bearing the image reference `](image_url)`.
    needle = f"]({image_url})"
    idx = next((i for i, ln in enumerate(lines) if needle in ln), None)
    if idx is None:
        return markdown

    block = _caption_block(description)
    # Determine the insertion point: right after the image line, skipping one blank.
    j = idx + 1
    if j < len(lines) and lines[j].strip() == "":
        j += 1
    # Replace an existing caption block for this image (a run of '> ' lines) if present.
    if j < len(lines) and lines[j].lstrip().startswith("> **Figure:**"):
        end = j
        while end < len(lines) and lines[end].lstrip().startswith(">"):
            end += 1
        lines[j:end] = [block]
        return "\n".join(lines)

    # Insert a fresh caption with a blank line on each side.
    insert_at = idx + 1
    lines[insert_at:insert_at] = ["", block]
    return "\n".join(lines)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/test_caption_inject.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/image_describe.py backend/tests/test_caption_inject.py
git commit -m "feat(images): idempotent inject_caption markdown helper"
```

---

## Task 5: `enrich_run_images` — the enrichment phase driver

**Files:**
- Modify: `app/services/image_describe.py`
- Test: `tests/test_image_enrich_phase.py`

**Interfaces:**
- Consumes: `evaluate_image`, `describe_image`, `inject_caption`; `ArticleImage`, `Article`, `ImageDescription`, `change_log.record_change`; `settings.image_vlm_*`, `settings.media_dir`.
- Produces: `async def enrich_run_images(db: AsyncSession, source_id: uuid.UUID, run_id: uuid.UUID, *, describe=describe_image) -> None`. Best-effort; never raises. `describe` is injectable for tests.

**Behavior (implement exactly):**
- No-op if `not settings.image_vlm_enabled`.
- Candidate images = `ArticleImage` rows of articles in `source_id` where the image is
  **not yet evaluated** (`is_meaningful IS NULL`) **or** meaningful-but-undescribed
  (`is_meaningful = true AND description IS NULL`). Grouped by article.
- Per candidate image: read bytes from `media_dir/<article_id>/<local_filename>` (missing
  file → skip). If `is_meaningful IS NULL`, evaluate (`to_thread(evaluate_image, data)`) and
  persist `is_meaningful/width/height/bytes_sha256`. If not meaningful → continue. If
  meaningful: resolve a description — **cache first** (`ImageDescription` by `bytes_sha256`);
  on miss, if budget remains and the breaker hasn't tripped, call `describe`, persist to the
  cache. On a `None` result, count a consecutive failure (break out after
  `image_vlm_max_consecutive_failures`). Set `img.description/kind`, `inject_caption` into
  `article.content_markdown`, mark the article changed.
- After processing an article's images: if changed, `record_change(change_type="updated")`
  and `db.commit()`. **Never** touch `article.content_hash`; **never** create an
  `ArticleVersion`.
- Budget (`image_vlm_max_per_run`) counts only NEW VLM calls (cache hits are free).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_image_enrich_phase.py`:

```python
"""enrich_run_images: describe → cache → caption → updated outbox row, best-effort."""
import io
import os
import sys
import uuid

import pytest
import pytest_asyncio
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core.database import Base
from app.models import Vendor, Product, DocumentationSource, ExtractionRun, Article
from app.models.image import ArticleImage
from app.models.image_description import ImageDescription
from app.models.content_change import ContentChange
from app.services import image_describe
from app.services.image_describe import enrich_run_images, ImageDescription as Desc

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def factory(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "image_vlm_enabled", True)
    monkeypatch.setattr(settings, "media_dir", str(tmp_path))
    monkeypatch.setattr(settings, "image_vlm_max_per_run", 100)
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    f = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield f
    await engine.dispose()


def _png(w, h, color=(20, 40, 60)):
    buf = io.BytesIO(); Image.new("RGB", (w, h), color).save(buf, format="PNG"); return buf.getvalue()


def _noise_png(w, h):
    # Noise doesn't compress, so it clears image_min_bytes (a solid-color PNG is
    # ~1 KB, under the threshold, and would be wrongly rejected as decorative).
    buf = io.BytesIO()
    Image.effect_noise((w, h), 100).convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


async def _seed_article_with_image(f, *, img_bytes, filename="x.png", md=None):
    async with f() as s:
        v = Vendor(name="V"); s.add(v); await s.flush()
        p = Product(name="P", vendor_id=v.id); s.add(p); await s.flush()
        src = DocumentationSource(name="S", base_url="https://x", product_id=p.id); s.add(src); await s.flush()
        run = ExtractionRun(source_id=src.id, status="running"); s.add(run); await s.flush()
        art = Article(
            source_id=src.id, extraction_run_id=run.id, created_run_id=run.id,
            title="A", source_url="https://x/a", topic_key="https://x/a",
            content_markdown=md or f"# A\n\n![pic](/media/PLACEHOLDER/{filename})\n\nBody.",
            content_hash="h-raw",
        )
        s.add(art); await s.flush()
        served = f"/media/{art.id}/{filename}"
        art.content_markdown = art.content_markdown.replace("/media/PLACEHOLDER/", f"/media/{art.id}/")
        img = ArticleImage(article_id=art.id, original_url="https://x/pic.png",
                           local_filename=filename, local_path=served, sort_order=0)
        s.add(img)
        await s.commit()
        # Write bytes to media_dir/<article.id>/<filename>
        d = os.path.join(settings.media_dir, str(art.id)); os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, filename), "wb") as fh:
            fh.write(img_bytes)
        return src.id, art.id, run.id


async def test_describes_and_captions_and_emits_updated_row(factory):
    src_id, art_id, run_id = await _seed_article_with_image(factory, img_bytes=_noise_png(400, 300))
    calls = {"n": 0}

    async def fake_describe(data, alt, **kw):
        calls["n"] += 1
        return Desc(text="A topology diagram.", kind="diagram")

    await enrich_run_images_via(factory, src_id, run_id, fake_describe)

    async with factory() as s:
        img = (await s.execute(select(ArticleImage).where(ArticleImage.article_id == art_id))).scalar_one()
        assert img.is_meaningful is True and img.description == "A topology diagram." and img.kind == "diagram"
        assert img.bytes_sha256 and img.width == 400 and img.height == 300
        art = await s.get(Article, art_id)
        assert "> **Figure:** A topology diagram." in art.content_markdown
        assert art.content_hash == "h-raw"  # untouched
        rows = (await s.execute(select(ContentChange).where(
            ContentChange.article_id == art_id, ContentChange.change_type == "updated"))).scalars().all()
        assert len(rows) == 1
        cached = await s.get(ImageDescription, img.bytes_sha256)
        assert cached is not None and cached.description == "A topology diagram."
    assert calls["n"] == 1


async def test_cache_hit_skips_vlm_call(factory):
    # Two articles sharing identical image bytes → described once, reused.
    img = _noise_png(400, 300)
    src_id, a1, run_id = await _seed_article_with_image(factory, img_bytes=img, filename="a.png")
    # second article in the same source with the same bytes
    async with factory() as s:
        art2 = Article(source_id=src_id, extraction_run_id=run_id, created_run_id=run_id,
                       title="B", source_url="https://x/b", topic_key="https://x/b",
                       content_markdown="# B\n\n![p](/media/PH/b.png)\n\nx.", content_hash="h2")
        s.add(art2); await s.flush()
        art2.content_markdown = art2.content_markdown.replace("/media/PH/", f"/media/{art2.id}/")
        s.add(ArticleImage(article_id=art2.id, original_url="u", local_filename="b.png",
                           local_path=f"/media/{art2.id}/b.png", sort_order=0))
        await s.commit()
        d = os.path.join(settings.media_dir, str(art2.id)); os.makedirs(d, exist_ok=True)
        open(os.path.join(d, "b.png"), "wb").write(img)

    calls = {"n": 0}
    async def fake_describe(data, alt, **kw):
        calls["n"] += 1
        return Desc(text="Shared image.", kind="photo")

    await enrich_run_images_via(factory, src_id, run_id, fake_describe)
    assert calls["n"] == 1  # described once despite two articles


async def test_idempotent_second_run_no_vlm_no_new_row(factory):
    src_id, art_id, run_id = await _seed_article_with_image(factory, img_bytes=_noise_png(400, 300))
    async def fake(data, alt, **kw):
        return Desc(text="Desc.", kind="screenshot")
    await enrich_run_images_via(factory, src_id, run_id, fake)

    calls = {"n": 0}
    async def counting(data, alt, **kw):
        calls["n"] += 1; return Desc(text="Desc.", kind="screenshot")
    await enrich_run_images_via(factory, src_id, run_id, counting)

    assert calls["n"] == 0  # all images already described
    async with factory() as s:
        rows = (await s.execute(select(ContentChange).where(
            ContentChange.article_id == art_id, ContentChange.change_type == "updated"))).scalars().all()
        assert len(rows) == 1  # only the first run's row


async def test_budget_cap_defers(factory, monkeypatch):
    monkeypatch.setattr(settings, "image_vlm_max_per_run", 1)
    src_id, a1, run_id = await _seed_article_with_image(factory, img_bytes=_noise_png(400, 300), filename="a.png")
    async with factory() as s:  # second article, distinct bytes
        art2 = Article(source_id=src_id, extraction_run_id=run_id, created_run_id=run_id,
                       title="B", source_url="https://x/b", topic_key="https://x/b",
                       content_markdown="![p](/media/PH/b.png)", content_hash="h2")
        s.add(art2); await s.flush()
        art2.content_markdown = f"![p](/media/{art2.id}/b.png)"
        s.add(ArticleImage(article_id=art2.id, original_url="u", local_filename="b.png",
                           local_path=f"/media/{art2.id}/b.png", sort_order=0))
        await s.commit()
        d = os.path.join(settings.media_dir, str(art2.id)); os.makedirs(d, exist_ok=True)
        open(os.path.join(d, "b.png"), "wb").write(_noise_png(400, 300))

    calls = {"n": 0}
    async def fake(data, alt, **kw):
        calls["n"] += 1; return Desc(text=f"d{calls['n']}", kind="other")
    await enrich_run_images_via(factory, src_id, run_id, fake)
    assert calls["n"] == 1  # budget of 1 honored; the other image deferred (still undescribed)


async def test_not_meaningful_image_no_row(factory):
    src_id, art_id, run_id = await _seed_article_with_image(factory, img_bytes=_png(30, 30))
    async def fake(data, alt, **kw):
        raise AssertionError("should not be called for a tiny image")
    await enrich_run_images_via(factory, src_id, run_id, fake)
    async with factory() as s:
        img = (await s.execute(select(ArticleImage).where(ArticleImage.article_id == art_id))).scalar_one()
        assert img.is_meaningful is False and img.description is None
        rows = (await s.execute(select(ContentChange).where(ContentChange.change_type == "updated"))).scalars().all()
        assert rows == []


# helper: run the phase on its own session against the test factory
async def enrich_run_images_via(factory, src_id, run_id, describe):
    async with factory() as db:
        await enrich_run_images(db, src_id, run_id, describe=describe)
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_image_enrich_phase.py -v`
Expected: FAIL — `enrich_run_images` not defined.

- [ ] **Step 3: Implement `enrich_run_images`**

Append to `app/services/image_describe.py` (add imports: `import asyncio`, `import os`, `import uuid`, `from sqlalchemy import select, or_`, `from sqlalchemy.ext.asyncio import AsyncSession`, `from app.models.article import Article`, `from app.models.image import ArticleImage`, `from app.services import change_log`, and — **aliased to avoid colliding with the `ImageDescription` dataclass from Task 3** — `from app.models.image_description import ImageDescription as ImageDescriptionCache`):

```python
def _needs_work(img: ArticleImage) -> bool:
    return img.is_meaningful is None or (img.is_meaningful and img.description is None)


async def enrich_run_images(db: AsyncSession, source_id, run_id, *, describe=describe_image) -> None:
    """Enrichment phase: describe meaningful images of this source's articles, inject
    captions, and emit an 'updated' content_changes row per enriched article. Best-effort
    — never raises into extraction; leaves content_hash and versions untouched."""
    if not settings.image_vlm_enabled:
        return
    try:
        budget = settings.image_vlm_max_per_run
        consecutive_failures = 0
        media_root = os.path.abspath(settings.media_dir)

        # Articles of this source with at least one image needing work.
        need = or_(
            ArticleImage.is_meaningful.is_(None),
            (ArticleImage.is_meaningful.is_(True)) & (ArticleImage.description.is_(None)),
        )
        art_ids = (await db.execute(
            select(Article.id)
            .where(Article.source_id == source_id)
            .where(Article.id.in_(select(ArticleImage.article_id).where(need)))
        )).scalars().all()

        for art_id in art_ids:
            article = await db.get(Article, art_id)
            if article is None:
                continue
            imgs = (await db.execute(
                select(ArticleImage).where(ArticleImage.article_id == art_id, need)
                .order_by(ArticleImage.sort_order)
            )).scalars().all()
            changed = False
            for img in imgs:
                path = os.path.join(media_root, str(art_id), img.local_filename)
                if not os.path.isfile(path):
                    continue
                data = await asyncio.to_thread(_read_file, path)
                if img.is_meaningful is None:
                    ev = await asyncio.to_thread(evaluate_image, data)
                    img.is_meaningful = ev.is_meaningful
                    img.width, img.height, img.bytes_sha256 = ev.width, ev.height, ev.bytes_sha256
                if not img.is_meaningful:
                    continue
                # Resolve a description: cache first, then VLM (budget + breaker).
                cached = await db.get(ImageDescriptionCache, img.bytes_sha256)
                if cached is not None:
                    text, kind = cached.description, cached.kind or "other"
                else:
                    if budget <= 0 or consecutive_failures >= settings.image_vlm_max_consecutive_failures:
                        continue
                    res = await describe(data, img.alt_text)
                    if res is None:
                        consecutive_failures += 1
                        continue
                    consecutive_failures = 0
                    budget -= 1
                    db.add(ImageDescriptionCache(
                        bytes_sha256=img.bytes_sha256, description=res.text,
                        kind=res.kind, model=settings.image_vlm_model,
                    ))
                    text, kind = res.text, res.kind
                img.description, img.kind = text, kind
                article.content_markdown = inject_caption(article.content_markdown, img.local_path, text)
                changed = True

            if changed:
                await change_log.record_change(db, article=article, change_type="updated", run_id=run_id)
            await db.commit()
            if consecutive_failures >= settings.image_vlm_max_consecutive_failures:
                break
    except Exception as exc:  # noqa: BLE001 — enrichment is best-effort
        logger.warning("enrich_run_images failed for source %s: %s", source_id, exc)


def _read_file(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_image_enrich_phase.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/image_describe.py backend/tests/test_image_enrich_phase.py
git commit -m "feat(images): enrich_run_images phase (describe, cache, caption, updated outbox row)"
```

---

## Task 6: Wire the phase into extraction + surface fields in the API

**Files:**
- Modify: `app/services/firecrawl.py` (call `enrich_run_images` in `extract_source`)
- Modify: `app/schemas/article.py` (`ArticleImageResponse`)
- Test: `tests/test_image_surfacing.py`

**Interfaces:**
- Consumes: `image_describe.enrich_run_images` (Task 5).
- Produces: `ArticleImageResponse` gains `description: str | None`, `kind: str | None`, `width: int | None`, `height: int | None`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_image_surfacing.py`:

```python
"""description/kind surface on GET /api/articles/{id} and the delta feed record."""
import json
import os
import sys

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core.database import Base, get_db
from app.main import app
from app.models import Vendor, Product, DocumentationSource, ExtractionRun, Article
from app.models.image import ArticleImage
from app.models.content_change import ContentChange

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def ctx():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    async def override_get_db():
        async with factory() as s:
            yield s

    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c, factory
    app.dependency_overrides.clear()
    await engine.dispose()


async def _seed_described(factory):
    async with factory() as s:
        v = Vendor(name="V"); s.add(v); await s.flush()
        p = Product(name="P", vendor_id=v.id); s.add(p); await s.flush()
        src = DocumentationSource(name="S", base_url="https://x", product_id=p.id); s.add(src); await s.flush()
        run = ExtractionRun(source_id=src.id, status="completed"); s.add(run); await s.flush()
        art = Article(source_id=src.id, extraction_run_id=run.id, created_run_id=run.id,
                      title="A", source_url="https://x/a", topic_key="https://x/a",
                      content_markdown="# A\n\n![p](/media/x/y.png)\n\n> **Figure:** A diagram.\n",
                      content_hash="h")
        s.add(art); await s.flush()
        s.add(ArticleImage(article_id=art.id, original_url="u", local_filename="y.png",
                           local_path="/media/x/y.png", sort_order=0,
                           is_meaningful=True, description="A diagram.", kind="diagram",
                           width=400, height=300, bytes_sha256="a"*64))
        s.add(ContentChange(article_id=art.id, source_id=src.id, run_id=run.id,
                            change_type="added", content_hash="h", topic_key="https://x/a"))
        await s.commit()
        return art.id


async def test_article_detail_exposes_description(ctx):
    c, factory = ctx
    art_id = await _seed_described(factory)
    resp = await c.get(f"/api/articles/{art_id}")
    assert resp.status_code == 200
    img = resp.json()["images"][0]
    assert img["description"] == "A diagram." and img["kind"] == "diagram"
    assert img["width"] == 400 and img["height"] == 300


async def test_delta_record_includes_description(ctx):
    c, factory = ctx
    await _seed_described(factory)
    resp = await c.get("/api/articles/delta")  # bootstrap
    lines = [json.loads(l) for l in resp.text.splitlines() if l.strip()]
    rec = next(r for r in lines if r.get("change_type") == "added")
    assert rec["images"][0]["description"] == "A diagram."
    assert rec["images"][0]["kind"] == "diagram"
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_image_surfacing.py -v`
Expected: FAIL — `ArticleImageResponse` has no `description` field (KeyError / missing key in JSON).

- [ ] **Step 3: Surface the fields on the response schema**

In `app/schemas/article.py`, extend `ArticleImageResponse`:

```python
class ArticleImageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    original_url: str
    local_filename: str
    alt_text: str | None
    file_size_bytes: int
    description: str | None = None
    kind: str | None = None
    width: int | None = None
    height: int | None = None
```

(The delta feed's `_images_payload` already reads `getattr(img, "description"/"kind", None)`, so those populate automatically once the columns exist.)

- [ ] **Step 4: Wire the enrichment phase into `extract_source`**

In `app/services/firecrawl.py`, add the import near the other service imports:

```python
from app.services import image_describe
```

In `extract_source`, immediately after `await self._reconcile_removals(db, source_id, run_pk)` (currently ~line 2113) and before `await checkpoint.clear()`, insert:

```python
            # Image enrichment phase (opt-in, best-effort): describe meaningful
            # images, inject captions, emit updated content_changes rows. Runs after
            # reconcile so removed pages are skipped; never fails the run.
            run.current_phase = "image_enrich"
            await db.commit()
            await image_describe.enrich_run_images(db, source_id, run_pk)
```

- [ ] **Step 5: Run the surfacing tests**

Run: `pytest tests/test_image_surfacing.py -v`
Expected: 2 passed.

- [ ] **Step 6: Regression — extraction, delta, and image suites**

Run: `pytest tests/test_image_evaluate.py tests/test_image_describe_vlm.py tests/test_caption_inject.py tests/test_image_enrich_phase.py tests/test_image_surfacing.py tests/test_delta_feed.py tests/test_change_log_wiring.py tests/test_incremental.py tests/test_integration.py -q`
Expected: all pass. (Enrichment is gated on `image_vlm_enabled=False` by default, so existing extraction tests are unaffected unless they enable it.)

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/firecrawl.py backend/app/schemas/article.py backend/tests/test_image_surfacing.py
git commit -m "feat(images): run enrichment phase in extract_source; surface description/kind on the API + delta feed"
```

---

## Self-Review

**Spec coverage:**
- Selection (boilerplate already filtered upstream; dimension/size/non-raster) → Task 2 `evaluate_image`. ✅
- VLM description service mirroring pdf_escalate config, base64 data URL, kind classification, `None` on failure → Task 3. ✅
- Cache by `bytes_sha256` (`image_descriptions`), described once ever → Task 1 (schema) + Task 5 (cache lookup/insert). ✅
- Budget + consecutive-failure circuit breaker; best-effort, never raises → Task 5. ✅
- Inline caption in markdown, idempotent; structured `images[].description`/`kind` → Task 4 + Task 5 + Task 6. ✅
- Enrichment as a post-scrape phase; `content_hash` untouched; no `ArticleVersion`; emits `updated` outbox row → Task 5 + Task 6. ✅
- No phantom churn on unchanged reruns (gate on `description IS NULL`) → Task 5 + `test_idempotent_second_run_no_vlm_no_new_row`. ✅
- Surfacing on `GET /api/articles/{id}` and the delta record → Task 6. ✅
- Heartbeat-safe (`asyncio.to_thread` for file read + Pillow) → Task 5. ✅
- Opt-in (`image_vlm_enabled=False` default) → Task 2 + Task 6 gating. ✅

**Placeholder scan:** No TBD/TODO; every code and test step is complete. ✅

**Type consistency:** `ImageEval`/`ImageDescription`/`evaluate_image`/`describe_image`/`inject_caption`/`enrich_run_images` signatures are consistent across Tasks 2–6. `enrich_run_images(..., describe=describe_image)` injection matches the test helper. New `ArticleImage` columns match the migration and the response schema. `image_descriptions` PK `bytes_sha256` matches the cache lookups. ✅

**Accepted trade-offs (documented in the spec):** a net-new enriched article emits an `added` (pre-caption) then an `updated` (captioned) row in the same run; the run's `updated` count / `run.articles_updated` counter don't include enrichment rows (the webhook `delta.updated`, computed from `content_changes`, does). SVG/non-raster images are never described (can't measure/rasterize). PDF figures are out of scope.
