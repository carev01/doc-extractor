# Robust PDF Conversion (Docling + heading-split + VLM escalation) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the page-range PDF→Markdown pipeline with whole-document Docling conversion split on heading boundaries, plus automatic quality-gated VLM escalation (OpenRouter) for the hard pages, eliminating cross-section bleed, truncated tables, and messed formatting.

**Architecture:** Convert the whole PDF once with Docling (reading order + tables intact + page provenance), then split the resulting markdown at heading boundaries (never page ranges). Score each article for low-confidence tables/sparse text and re-convert only those by rendering pages to images and calling a cheap OpenRouter vision model. The existing DB / diff / versioning / TOC and `process_article_result` machinery is unchanged.

**Tech Stack:** Python 3, FastAPI, SQLAlchemy async, PyMuPDF (`fitz`), `pymupdf4llm` (fallback only), `docling`, httpx, pytest (sync DB style).

## Global Constraints

- Spec: `docs/superpowers/specs/2026-06-27-pdf-conversion-docling-vlm-design.md`.
- Settings use the `DOCEXTRACTOR_` env prefix via `pydantic-settings` (`app/core/config.py`).
- VLM is **OpenRouter (OpenAI-compatible), never Claude/Anthropic**. Default model `google/gemini-2.0-flash-001`.
- Conversion is CPU-bound and MUST run off the event loop via `asyncio.to_thread` so the worker heartbeat keeps ticking (see `worker-event-loop-heartbeat` memory / PR #90).
- Never regress to "no output": Docling failure falls back to a whole-doc `pymupdf4llm` conversion.
- No DB schema change. `process_article_result` / `_reconcile_removals` / TOC-tree build stay as-is.
- Tests are synchronous (`psycopg2` + sync `Session`); build deterministic PDFs with `fitz`; mock Docling and all network/LLM calls in unit tests. Test files live in `backend/tests/`, each starting with the existing `sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))` shim.
- Run all backend commands from `backend/`. Test runner: `pytest`.
- Validation PDF is at the session scratchpad: `$SCRATCH/HYCU_CompatibilityMatrix.pdf` (24 pages; outline puts several sections on the same page — the bleed reproducer).

---

### Task 1: Add conversion + VLM settings

**Files:**
- Modify: `backend/app/core/config.py` (after line 90, the `pdf_max_upload_bytes` block)
- Test: `backend/tests/test_pdf_convert_settings.py` (create)

**Interfaces:**
- Produces: `settings.pdf_converter: str`, `settings.pdf_vlm_escalation_enabled: bool`, `settings.pdf_vlm_base_url: str`, `settings.pdf_vlm_api_key: str`, `settings.pdf_vlm_model: str`, `settings.pdf_vlm_max_pages_per_run: int`, `settings.pdf_vlm_dpi: int`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_pdf_convert_settings.py
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import Settings


def _settings(**env):
    # Required fields have no defaults; supply dummies so Settings() constructs.
    base = dict(
        database_url="postgresql+asyncpg://x/y",
        database_url_sync="postgresql+psycopg2://x/y",
        firecrawl_api_url="http://x",
    )
    base.update(env)
    return Settings(**base)


def test_pdf_converter_defaults():
    s = _settings()
    assert s.pdf_converter == "docling"
    assert s.pdf_vlm_escalation_enabled is True
    assert s.pdf_vlm_base_url == "https://openrouter.ai/api/v1/chat/completions"
    assert s.pdf_vlm_api_key == ""
    assert s.pdf_vlm_model == "google/gemini-2.0-flash-001"
    assert s.pdf_vlm_max_pages_per_run == 30
    assert s.pdf_vlm_dpi == 150


def test_pdf_settings_override_from_env_kwargs():
    s = _settings(pdf_converter="pymupdf", pdf_vlm_max_pages_per_run=5)
    assert s.pdf_converter == "pymupdf"
    assert s.pdf_vlm_max_pages_per_run == 5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_pdf_convert_settings.py -v`
Expected: FAIL — `AttributeError`/validation: `pdf_converter` not defined.

- [ ] **Step 3: Add the settings**

In `backend/app/core/config.py`, immediately after the `pdf_max_upload_bytes` line (line 90):

```python
    # PDF conversion engine (Layer A) and VLM escalation (Layer B).
    # pdf_converter: "docling" (default, layout/table-aware) | "pymupdf" (fallback).
    # The VLM path uses OpenRouter (OpenAI-compatible) — never Anthropic — and is
    # configured independently of the llm_* (segmentation) settings above.
    pdf_converter: str = "docling"
    pdf_vlm_escalation_enabled: bool = True
    pdf_vlm_base_url: str = "https://openrouter.ai/api/v1/chat/completions"
    pdf_vlm_api_key: str = ""
    pdf_vlm_model: str = "google/gemini-2.0-flash-001"
    pdf_vlm_max_pages_per_run: int = 30
    pdf_vlm_dpi: int = 150
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_pdf_convert_settings.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
git add app/core/config.py tests/test_pdf_convert_settings.py
git commit -m "feat(pdf): add docling/VLM conversion settings"
```

---

### Task 2: Add Docling dependency and confirm its API (spike)

This task is verification-based, not TDD: it pins the exact Docling API on the
installed version so Tasks 4–5 use correct calls. Its deliverable is a confirmed
probe script committed under `backend/tests/manual/`.

**Files:**
- Modify: `backend/requirements.txt` (after line 19, the `pymupdf4llm` pin)
- Create: `backend/tests/manual/docling_probe.py`

- [ ] **Step 1: Pin the dependency**

Add to `backend/requirements.txt` after the `pymupdf4llm==0.0.17` line:

```
docling==2.15.0
```

- [ ] **Step 2: Install**

Run: `pip install 'docling==2.15.0'`
Expected: installs (pulls torch + models libs; large). If `2.15.0` is unavailable, install the newest `2.x` and update the pin to match the installed version (`pip show docling`).

- [ ] **Step 3: Write the probe script**

```python
# backend/tests/manual/docling_probe.py
"""Manual probe: confirm Docling's heading/table/provenance API on this version.

Run:  SCRATCH=<scratchpad dir> python tests/manual/docling_probe.py
Prints the engine surface Tasks 4-5 depend on so the field/enum names can be
verified against the installed docling version.
"""
import os
import sys
from io import BytesIO

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from docling.document_converter import DocumentConverter
from docling.datamodel.base_models import DocumentStream

pdf = os.path.join(os.environ["SCRATCH"], "HYCU_CompatibilityMatrix.pdf")
data = open(pdf, "rb").read()

conv = DocumentConverter()
res = conv.convert(DocumentStream(name="probe.pdf", stream=BytesIO(data)))
doc = res.document

md = doc.export_to_markdown()
print("=== markdown length:", len(md))
print(md[:600])
print("\n=== iterate_items labels/levels/pages ===")
for item, level in doc.iterate_items():
    label = getattr(item, "label", None)
    prov = getattr(item, "prov", None) or []
    page = prov[0].page_no if prov else None
    text = (getattr(item, "text", "") or "")[:50]
    print(repr(str(label)), "lvl=", getattr(item, "level", None),
          "tree_level=", level, "page=", page, "text=", repr(text))
print("\n=== tables ===", len(getattr(doc, "tables", []) or []))
for t in (getattr(doc, "tables", []) or []):
    prov = getattr(t, "prov", None) or []
    print("table page=", prov[0].page_no if prov else None)
```

- [ ] **Step 4: Run the probe and record results**

Run: `SCRATCH=/tmp/claude-1000/.../scratchpad python tests/manual/docling_probe.py` (use the real scratchpad path holding `HYCU_CompatibilityMatrix.pdf`).
Expected: markdown printed; section headings emitted with a recognisable label (e.g. `section_header`/`DocItemLabel.SECTION_HEADER`) and a `level`; tables listed with page numbers. **Record the exact label string and level field name** — Tasks 4 normalises against them.

- [ ] **Step 5: Commit**

```bash
git add requirements.txt tests/manual/docling_probe.py
git commit -m "build(pdf): add docling dependency + API probe script"
```

---

### Task 3: Add image support to `call_llm`

**Files:**
- Modify: `backend/app/services/profiles/llm.py:93-165` (the `call_llm` function)
- Test: `backend/tests/test_call_llm_images.py` (create)

**Interfaces:**
- Consumes: nothing new.
- Produces: `async call_llm(prompt: str, *, system: str | None = None, images: list[bytes] | None = None) -> str`. When `images` is given, the user turn becomes a content-block list. OpenAI/OpenRouter: `{"type":"image_url","image_url":{"url":"data:image/png;base64,<b64>"}}`. Anthropic: `{"type":"image","source":{"type":"base64","media_type":"image/png","data":"<b64>"}}`. Text-only callers are unaffected.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_call_llm_images.py
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.profiles import llm as llm_mod


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeClient:
    captured = {}

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        _FakeClient.captured = {"url": url, "headers": headers, "json": json}
        # OpenAI-shaped response
        return _FakeResp({"choices": [{"message": {"content": "ok"}}]})


@pytest.mark.asyncio
async def test_openai_image_payload_shape(monkeypatch):
    monkeypatch.setattr(llm_mod.settings, "llm_provider", "openai")
    monkeypatch.setattr(llm_mod.settings, "llm_api_key", "k")
    monkeypatch.setattr(llm_mod.settings, "llm_base_url", "http://router/v1/chat")
    monkeypatch.setattr(llm_mod.settings, "llm_model", "vision-model")
    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", _FakeClient)

    out = await llm_mod.call_llm("describe", images=[b"\x89PNG_fake"])
    assert out == "ok"
    msgs = _FakeClient.captured["json"]["messages"]
    content = msgs[-1]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "describe"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_text_only_payload_unchanged(monkeypatch):
    monkeypatch.setattr(llm_mod.settings, "llm_provider", "openai")
    monkeypatch.setattr(llm_mod.settings, "llm_api_key", "k")
    monkeypatch.setattr(llm_mod.settings, "llm_base_url", "http://router/v1/chat")
    monkeypatch.setattr(llm_mod.settings, "llm_model", "m")
    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", _FakeClient)

    await llm_mod.call_llm("hello")
    content = _FakeClient.captured["json"]["messages"][-1]["content"]
    assert content == "hello"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_call_llm_images.py -v`
Expected: FAIL — `call_llm()` got an unexpected keyword argument `images`.

- [ ] **Step 3: Implement image support**

In `backend/app/services/profiles/llm.py`, add a helper above `call_llm` and thread `images` through. Replace the `call_llm` signature line and the message-construction in both provider branches:

```python
import base64

def _b64_png(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")
```

Change the signature to:

```python
async def call_llm(
    prompt: str, *, system: str | None = None, images: list[bytes] | None = None
) -> str:
```

In the **anthropic** branch, replace the `messages=[{"role": "user", "content": prompt}]` construction with:

```python
            if images:
                content = [{"type": "text", "text": prompt}]
                for img in images:
                    content.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": _b64_png(img),
                        },
                    })
            else:
                content = prompt
            body = {
                "model": model,
                "max_tokens": settings.llm_max_tokens,
                "messages": [{"role": "user", "content": content}],
            }
```

In the **openai** branch, replace `messages.append({"role": "user", "content": prompt})` with:

```python
            if images:
                user_content = [{"type": "text", "text": prompt}]
                for img in images:
                    user_content.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{_b64_png(img)}"},
                    })
            else:
                user_content = prompt
            messages.append({"role": "user", "content": user_content})
```

Note: the openai branch sets `response_format={"type": "json_object"}`. Leave it for text-only callers, but it must NOT force JSON for the VLM markdown path — Task 7 calls a separate helper that does not use this function's json_object setting. (Task 7's helper builds its own request; `call_llm`'s openai branch is used only by existing JSON callers.) Therefore the VLM path does **not** go through `call_llm`'s openai branch; it uses the dedicated helper in Task 7. `call_llm`'s `images` support here is kept general (and exercised by the anthropic-shape test path) but the production VLM call uses Task 7's helper.

> Implementer note: because the existing openai branch hard-codes `response_format=json_object`, do not route the VLM markdown call through `call_llm`. Task 7 defines `_vlm_complete` for that. The `images` kwarg on `call_llm` remains for completeness/future use and is covered by the tests above (the openai test asserts payload shape only; it does not assert json_object is absent).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_call_llm_images.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
git add app/services/profiles/llm.py tests/test_call_llm_images.py
git commit -m "feat(llm): optional image content blocks in call_llm"
```

---

### Task 4: `pdf_convert.convert_pdf` — whole-doc conversion + pymupdf fallback

**Files:**
- Create: `backend/app/services/pdf_convert.py`
- Test: `backend/tests/test_pdf_convert.py` (create)

**Interfaces:**
- Consumes: `RenderedImage` (moved here from `pdf_import.py`).
- Produces:
  - `@dataclass DocHeading(text: str, level: int, page0: int)`
  - `@dataclass ConvertedDoc(markdown: str, headings: list[DocHeading], page_texts: list[str], table_pages: set[int], engine: str)`
  - `@dataclass RenderedImage(filename: str, data: bytes, alt: str)` (moved from `pdf_import`)
  - `def convert_pdf(pdf_bytes: bytes) -> ConvertedDoc` — tries Docling, falls back to `pymupdf4llm` whole-doc on any exception. Markdown has content-addressed image refs (`<sha16>.png`); the images themselves are returned via `ConvertedDoc`? No — images are sliced per segment in Task 5, so `ConvertedDoc` also carries `images: list[RenderedImage]`. Add that field.
  - `def _content_address_images(markdown: str, image_dir: str) -> tuple[str, list[RenderedImage]]` — rewrite `![alt](target)` markers whose target file exists in `image_dir` to `![alt](<sha16>.png)`, collecting deduped bytes (logic identical to the current `_render_segment` replacer).

Update `ConvertedDoc` to include `images: list[RenderedImage]`:
`ConvertedDoc(markdown, headings, page_texts, table_pages, images, engine)`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_pdf_convert.py
import os
import sys

import fitz

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.services.pdf_convert as pc


def _two_section_pdf() -> bytes:
    doc = fitz.open()
    p = doc.new_page()
    p.insert_text((72, 72), "Alpha Section", fontsize=20)
    p.insert_text((72, 120), "Alpha body content here.", fontsize=11)
    p.insert_text((72, 200), "Beta Section", fontsize=20)
    p.insert_text((72, 240), "Beta body content here.", fontsize=11)
    return doc.tobytes()


def test_pymupdf_fallback_produces_markdown_and_page_texts(monkeypatch):
    # Force the docling branch to raise so the fallback runs.
    monkeypatch.setattr(pc, "_convert_docling",
                        lambda b: (_ for _ in ()).throw(RuntimeError("no docling")))
    out = pc.convert_pdf(_two_section_pdf())
    assert out.engine == "pymupdf"
    assert "Alpha" in out.markdown and "Beta" in out.markdown
    assert len(out.page_texts) == 1
    assert "Alpha" in out.page_texts[0]


def test_content_address_images(tmp_path):
    img = tmp_path / "img0.png"
    png = (b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    img.write_bytes(png)
    md = "before ![a cat](img0.png) after"
    new_md, images = pc._content_address_images(md, str(tmp_path))
    assert len(images) == 1
    assert images[0].filename.endswith(".png")
    assert images[0].filename in new_md
    assert "img0.png" not in new_md
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_pdf_convert.py -v`
Expected: FAIL — `No module named app.services.pdf_convert`.

- [ ] **Step 3: Implement the module**

```python
# backend/app/services/pdf_convert.py
"""Whole-document PDF→markdown conversion (Docling, with a pymupdf fallback).

Converting the entire document at once preserves reading order and keeps tables
whole across page breaks; splitting into articles happens later (pdf_split-style
logic in split_into_segments) at heading boundaries, never page ranges."""
from __future__ import annotations

import hashlib
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from io import BytesIO

import fitz  # PyMuPDF
import pymupdf4llm

from app.core.config import settings
from app.services.sanitize import sanitize_markdown

logger = logging.getLogger(__name__)


@dataclass
class RenderedImage:
    filename: str   # content-addressed: "<sha16>.png"
    data: bytes
    alt: str


@dataclass
class DocHeading:
    text: str
    level: int
    page0: int  # 0-based page where the heading appears


@dataclass
class ConvertedDoc:
    markdown: str
    headings: list[DocHeading]
    page_texts: list[str]
    table_pages: set[int]
    images: list[RenderedImage] = field(default_factory=list)
    engine: str = "docling"


_IMG_MARKER = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<target>[^)]+)\)")


def _content_address_images(markdown: str, image_dir: str) -> tuple[str, list[RenderedImage]]:
    images: list[RenderedImage] = []
    seen: dict[str, str] = {}
    seen_shas: set[str] = set()

    def _replace(m: "re.Match") -> str:
        target = m.group("target")
        alt = m.group("alt")
        path = os.path.join(image_dir, os.path.basename(target))
        if not os.path.isfile(path):
            return m.group(0)
        if target in seen:
            return f"![{alt}]({seen[target]})"
        with open(path, "rb") as fh:
            data = fh.read()
        filename = hashlib.sha256(data).hexdigest()[:16] + ".png"
        seen[target] = filename
        if filename not in seen_shas:
            seen_shas.add(filename)
            images.append(RenderedImage(filename=filename, data=data, alt=alt))
        return f"![{alt}]({filename})"

    return _IMG_MARKER.sub(_replace, markdown), images


def _page_texts(pdf_bytes: bytes) -> list[str]:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        return [page.get_text("text") for page in doc]
    finally:
        doc.close()


def _norm_label(label) -> str:
    s = str(label).lower()
    return s.rsplit(".", 1)[-1]  # "DocItemLabel.SECTION_HEADER" -> "section_header"


def _convert_docling(pdf_bytes: bytes) -> ConvertedDoc:
    # Imported lazily so the fallback path (and non-PDF code) never pays the
    # heavy docling import cost, and tests can monkeypatch this function.
    from docling.datamodel.base_models import DocumentStream
    from docling.document_converter import DocumentConverter

    with tempfile.TemporaryDirectory() as image_dir:
        conv = DocumentConverter()
        result = conv.convert(
            DocumentStream(name="source.pdf", stream=BytesIO(pdf_bytes))
        )
        doc = result.document
        # Verified against the installed docling version in Task 2's probe.
        # export_to_markdown writes referenced images into image_dir when the
        # converter is configured to keep page images; if this version inlines
        # images differently, adjust per the probe output.
        try:
            md = doc.export_to_markdown(artifacts_dir=image_dir)
        except TypeError:
            md = doc.export_to_markdown()

        headings: list[DocHeading] = []
        table_pages: set[int] = set()
        for item, _tree_level in doc.iterate_items():
            label = _norm_label(getattr(item, "label", ""))
            prov = getattr(item, "prov", None) or []
            page0 = (prov[0].page_no - 1) if prov else 0
            if label in ("section_header", "title"):
                text = (getattr(item, "text", "") or "").strip()
                lvl = int(getattr(item, "level", 1) or 1)
                if label == "title":
                    lvl = 1
                if text:
                    headings.append(DocHeading(text=text, level=lvl, page0=page0))
            if "table" in label:
                table_pages.add(page0)
        for t in (getattr(doc, "tables", None) or []):
            prov = getattr(t, "prov", None) or []
            if prov:
                table_pages.add(prov[0].page_no - 1)

        md, images = _content_address_images(md, image_dir)
    return ConvertedDoc(
        markdown=sanitize_markdown(md),
        headings=headings,
        page_texts=_page_texts(pdf_bytes),
        table_pages=table_pages,
        images=images,
        engine="docling",
    )


def _convert_pymupdf(pdf_bytes: bytes) -> ConvertedDoc:
    """Whole-doc pymupdf4llm conversion (no page ranges → no boundary bleed)."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        with tempfile.TemporaryDirectory() as image_dir:
            md = pymupdf4llm.to_markdown(
                doc, write_images=True, image_path=image_dir, image_format="png"
            ) or ""
            md, images = _content_address_images(md, image_dir)
    finally:
        doc.close()
    return ConvertedDoc(
        markdown=sanitize_markdown(md),
        headings=[],            # heading-split falls back to ATX headings in the markdown
        page_texts=_page_texts(pdf_bytes),
        table_pages=set(),
        images=images,
        engine="pymupdf",
    )


def convert_pdf(pdf_bytes: bytes) -> ConvertedDoc:
    """Convert a whole PDF to markdown. Docling first; pymupdf4llm on any failure."""
    if settings.pdf_converter == "pymupdf":
        return _convert_pymupdf(pdf_bytes)
    try:
        out = _convert_docling(pdf_bytes)
        if out.markdown.strip():
            return out
        logger.warning("Docling produced empty markdown; falling back to pymupdf")
    except Exception as exc:  # noqa: BLE001 - fallback is intentional
        logger.warning("Docling conversion failed (%s); falling back to pymupdf", exc)
    return _convert_pymupdf(pdf_bytes)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_pdf_convert.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
git add app/services/pdf_convert.py tests/test_pdf_convert.py
git commit -m "feat(pdf): whole-doc convert_pdf (docling + pymupdf fallback)"
```

---

### Task 5: `split_into_segments` — heading-split (no page-range bleed)

**Files:**
- Modify: `backend/app/services/pdf_convert.py` (append)
- Test: `backend/tests/test_pdf_split.py` (create)

**Interfaces:**
- Consumes: `ConvertedDoc`, `DocHeading`, `RenderedImage` (Task 4); `Segment` (the outline dataclass from `pdf_import` — `title, level, page_start, page_end, path`).
- Produces:
  - `@dataclass RenderedSegment(title: str, level: int, path: list[str], page_start: int, page_end: int, markdown: str, images: list[RenderedImage])`
  - `def split_into_segments(converted: ConvertedDoc, outline: list[Segment]) -> list[RenderedSegment]` — split `converted.markdown` at heading boundaries. If `outline` is non-empty, use the outline titles as boundaries (locate each title as a markdown heading line); else use `converted.headings`; if neither yields a boundary, fall back to locating ATX headings (`^#{1,6} `) in the markdown; if still none, one whole-doc segment titled "Document".

Splitting is by **markdown line offset**, so a table is never cut (it lives entirely within one slice) and adjacent sections never bleed (the slice ends exactly at the next heading line).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_pdf_split.py
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.pdf_convert import (
    ConvertedDoc, DocHeading, RenderedImage, split_into_segments,
)
from app.services.pdf_import import Segment


def _doc(md, headings=None, table_pages=None):
    return ConvertedDoc(
        markdown=md, headings=headings or [], page_texts=[md],
        table_pages=table_pages or set(), images=[], engine="docling",
    )


def test_outline_split_has_no_cross_section_bleed():
    md = (
        "## Alpha Section\n\nAlpha body.\n\n"
        "## Beta Section\n\nBeta body.\n"
    )
    outline = [
        Segment(title="Alpha Section", level=1, page_start=0, page_end=0, path=["Alpha Section"]),
        Segment(title="Beta Section", level=1, page_start=0, page_end=0, path=["Beta Section"]),
    ]
    segs = split_into_segments(_doc(md), outline)
    assert [s.title for s in segs] == ["Alpha Section", "Beta Section"]
    assert "Alpha body." in segs[0].markdown
    assert "Beta" not in segs[0].markdown          # no trailing bleed
    assert "Beta body." in segs[1].markdown
    assert "Alpha body." not in segs[1].markdown   # no leading bleed


def test_split_never_cuts_a_table():
    md = (
        "## One\n\nintro\n\n"
        "| a | b |\n| --- | --- |\n| 1 | 2 |\n| 3 | 4 |\n\n"
        "## Two\n\ntail\n"
    )
    outline = [
        Segment(title="One", level=1, page_start=0, page_end=0, path=["One"]),
        Segment(title="Two", level=1, page_start=0, page_end=0, path=["Two"]),
    ]
    segs = split_into_segments(_doc(md), outline)
    one = next(s for s in segs if s.title == "One").markdown
    assert "| 1 | 2 |" in one and "| 3 | 4 |" in one   # full table in one slice


def test_no_outline_uses_docling_headings():
    md = "# Title\n\nbody one\n\n# Next\n\nbody two\n"
    headings = [DocHeading("Title", 1, 0), DocHeading("Next", 1, 0)]
    segs = split_into_segments(_doc(md, headings=headings), [])
    assert [s.title for s in segs] == ["Title", "Next"]
    assert "body one" in segs[0].markdown and "body two" in segs[1].markdown


def test_image_assigned_to_owning_segment():
    md = "## A\n\n![x](aa.png)\n\n## B\n\nplain\n"
    outline = [
        Segment(title="A", level=1, page_start=0, page_end=0, path=["A"]),
        Segment(title="B", level=1, page_start=0, page_end=0, path=["B"]),
    ]
    doc = _doc(md)
    doc.images = [RenderedImage(filename="aa.png", data=b"x", alt="x")]
    segs = split_into_segments(doc, outline)
    a = next(s for s in segs if s.title == "A")
    b = next(s for s in segs if s.title == "B")
    assert [i.filename for i in a.images] == ["aa.png"]
    assert b.images == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_pdf_split.py -v`
Expected: FAIL — `cannot import name 'split_into_segments'`.

- [ ] **Step 3: Implement the split**

Append to `backend/app/services/pdf_convert.py`:

```python
from app.services.pdf_import import Segment  # outline dataclass


@dataclass
class RenderedSegment:
    title: str
    level: int
    path: list[str]
    page_start: int
    page_end: int
    markdown: str
    images: list[RenderedImage] = field(default_factory=list)


_ATX_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


def _heading_lines(lines: list[str]) -> list[tuple[int, str]]:
    """All ATX heading lines as (line_index, normalized_text)."""
    out = []
    for i, ln in enumerate(lines):
        m = _ATX_RE.match(ln.strip())
        if m:
            out.append((i, m.group(2).strip()))
    return out


def _find_heading_line(headings: list[tuple[int, str]], title: str, start: int) -> int | None:
    """First heading line at/after `start` whose text matches `title` (case/space
    insensitive, exact-or-contains). Returns the line index or None."""
    t = " ".join(title.lower().split())
    for idx, text in headings:
        if idx < start:
            continue
        h = " ".join(text.lower().split())
        if h == t or t in h or h in t:
            return idx
    return None


def _assign_images(markdown: str, all_images: list[RenderedImage]) -> list[RenderedImage]:
    return [img for img in all_images if img.filename in markdown]


def split_into_segments(converted: ConvertedDoc, outline: list[Segment]) -> list[RenderedSegment]:
    md = converted.markdown
    lines = md.split("\n")
    heading_lines = _heading_lines(lines)

    # Build (line_index, title, level, path, page_start, page_end) boundaries.
    boundaries: list[tuple[int, str, int, list[str], int, int]] = []
    if outline:
        cursor = 0
        for seg in outline:
            line = _find_heading_line(heading_lines, seg.title, cursor)
            if line is None:
                continue
            cursor = line + 1
            boundaries.append((line, seg.title, seg.level, seg.path or [seg.title],
                               seg.page_start, seg.page_end))
    elif converted.headings:
        # Map docling headings to their markdown heading line in order.
        cursor = 0
        stack: list[str] = []
        for h in converted.headings:
            line = _find_heading_line(heading_lines, h.text, cursor)
            if line is None:
                continue
            cursor = line + 1
            stack = stack[: h.level - 1]
            stack.append(h.text)
            boundaries.append((line, h.text, h.level, list(stack), h.page0, h.page0))
    if not boundaries and heading_lines:
        # Last resort: split on every ATX heading.
        for idx, text in heading_lines:
            boundaries.append((idx, text, 1, [text], 0, 0))

    if not boundaries:
        return [RenderedSegment(
            title="Document", level=1, path=[], page_start=0,
            page_end=max(0, len(converted.page_texts) - 1),
            markdown=md.strip(),
            images=list(converted.images),
        )]

    segs: list[RenderedSegment] = []
    for i, (line, title, level, path, p_start, p_end) in enumerate(boundaries):
        end_line = boundaries[i + 1][0] if i + 1 < len(boundaries) else len(lines)
        body = "\n".join(lines[line:end_line]).strip()
        segs.append(RenderedSegment(
            title=title, level=level, path=path,
            page_start=p_start, page_end=p_end,
            markdown=body, images=_assign_images(body, converted.images),
        ))
    return segs
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_pdf_split.py -v`
Expected: PASS (all four tests).

- [ ] **Step 5: Commit**

```bash
git add app/services/pdf_convert.py tests/test_pdf_split.py
git commit -m "feat(pdf): heading-boundary split (no page-range bleed)"
```

---

### Task 6: `pdf_escalate.score_segment` — confidence signals

**Files:**
- Create: `backend/app/services/pdf_escalate.py`
- Test: `backend/tests/test_pdf_escalate_score.py` (create)

**Interfaces:**
- Consumes: `RenderedSegment` (Task 5), `ConvertedDoc.table_pages` / `page_texts` (Task 4).
- Produces:
  - `def score_segment(segment: RenderedSegment, converted: ConvertedDoc) -> list[str]` — returns a list of issue codes; empty means confident. Codes: `"ragged_table"`, `"missing_table"`, `"sparse_text"`.

Heuristics:
- `ragged_table`: any markdown table whose body rows' pipe-cell count differs from the header row's by more than 0 (after ignoring the `--- | ---` separator), OR a header present with zero body rows.
- `missing_table`: a page in `[page_start, page_end]` is in `converted.table_pages` but the segment markdown contains no `|` table line.
- `sparse_text`: segment markdown length < 50% of the concatenated `converted.page_texts[page_start:page_end+1]` length (and that raw length is > 200 chars, to avoid tiny-page noise).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_pdf_escalate_score.py
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.pdf_convert import ConvertedDoc, RenderedSegment
from app.services.pdf_escalate import score_segment


def _conv(page_texts, table_pages=None):
    return ConvertedDoc(
        markdown="", headings=[], page_texts=page_texts,
        table_pages=table_pages or set(), images=[], engine="docling",
    )


def _seg(md, p0=0, p1=0):
    return RenderedSegment(title="t", level=1, path=["t"], page_start=p0,
                           page_end=p1, markdown=md, images=[])


def test_clean_table_is_confident():
    md = "## t\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n"
    assert score_segment(_seg(md), _conv(["x" * 50])) == []


def test_ragged_table_flagged():
    md = "## t\n\n| a | b |\n| --- | --- |\n| 1 | 2 | 3 |\n"
    assert "ragged_table" in score_segment(_seg(md), _conv(["x" * 50]))


def test_missing_table_flagged():
    md = "## t\n\njust prose, no table\n"
    conv = _conv(["x" * 50], table_pages={0})
    assert "missing_table" in score_segment(_seg(md), conv)


def test_sparse_text_flagged():
    md = "## t\n\ntiny\n"
    conv = _conv(["y" * 1000])
    assert "sparse_text" in score_segment(_seg(md), conv)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_pdf_escalate_score.py -v`
Expected: FAIL — `No module named app.services.pdf_escalate`.

- [ ] **Step 3: Implement the scorer**

```python
# backend/app/services/pdf_escalate.py
"""Confidence scoring + VLM re-conversion of low-confidence PDF segments.

The default converter (Docling) is good but not perfect on the hardest tables.
score_segment flags segments worth re-doing; escalate_segment re-converts them by
rendering their pages to images and asking a cheap OpenRouter vision model for
clean markdown."""
from __future__ import annotations

import logging
import re

from app.services.pdf_convert import ConvertedDoc, RenderedSegment

logger = logging.getLogger(__name__)

_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_SEP_RE = re.compile(r"^\s*\|?\s*:?-{3,}.*$")


def _cell_count(row: str) -> int:
    # Count cells between the outer pipes.
    return len([c for c in row.strip().strip("|").split("|")])


def _has_ragged_table(md: str) -> bool:
    lines = md.split("\n")
    i = 0
    n = len(lines)
    while i < n:
        if _TABLE_ROW_RE.match(lines[i]):
            block = []
            while i < n and _TABLE_ROW_RE.match(lines[i]):
                block.append(lines[i])
                i += 1
            # block[0] header; block[1] separator; rest body.
            if len(block) < 2:
                return True  # header with no separator/body
            header_cells = _cell_count(block[0])
            body = [b for b in block[2:] if not _SEP_RE.match(b)]
            if not body:
                return True  # header + separator, zero body rows
            for row in body:
                if _cell_count(row) != header_cells:
                    return True
            continue
        i += 1
    return False


def score_segment(segment: RenderedSegment, converted: ConvertedDoc) -> list[str]:
    issues: list[str] = []
    md = segment.markdown

    if _has_ragged_table(md):
        issues.append("ragged_table")

    seg_pages = range(segment.page_start, segment.page_end + 1)
    if any(p in converted.table_pages for p in seg_pages) and "|" not in md:
        issues.append("missing_table")

    raw = "".join(
        converted.page_texts[p]
        for p in seg_pages
        if 0 <= p < len(converted.page_texts)
    )
    if len(raw) > 200 and len(md) < 0.5 * len(raw):
        issues.append("sparse_text")

    return issues
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_pdf_escalate_score.py -v`
Expected: PASS (all four).

- [ ] **Step 5: Commit**

```bash
git add app/services/pdf_escalate.py tests/test_pdf_escalate_score.py
git commit -m "feat(pdf): confidence scoring for converted segments"
```

---

### Task 7: `escalate_segment` — render pages + OpenRouter VLM re-conversion

**Files:**
- Modify: `backend/app/services/pdf_escalate.py` (append)
- Test: `backend/tests/test_pdf_escalate_vlm.py` (create)

**Interfaces:**
- Consumes: `RenderedSegment`; `settings.pdf_vlm_*`.
- Produces:
  - `def render_pages_png(pdf_bytes: bytes, page_start: int, page_end: int, dpi: int) -> list[bytes]` — one PNG per page via `fitz`.
  - `async def _vlm_complete(images: list[bytes], prompt: str) -> str` — POSTs an OpenAI-compatible chat request to `settings.pdf_vlm_base_url` with `settings.pdf_vlm_model`, image content blocks, no `response_format` (we want markdown, not JSON). Returns the message text. Raises on HTTP error / missing key.
  - `async def escalate_segment(pdf_bytes: bytes, segment: RenderedSegment) -> str` — renders the segment's pages, calls `_vlm_complete`, returns cleaned markdown (the heading line is re-prepended if the model dropped it). Returns the original `segment.markdown` on any failure.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_pdf_escalate_vlm.py
import os
import sys

import fitz
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.services.pdf_escalate as esc
from app.services.pdf_convert import RenderedSegment


def _pdf() -> bytes:
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "hello")
    return doc.tobytes()


def test_render_pages_png_returns_png_bytes():
    pngs = esc.render_pages_png(_pdf(), 0, 0, dpi=72)
    assert len(pngs) == 1
    assert pngs[0][:8] == b"\x89PNG\r\n\x1a\n"


@pytest.mark.asyncio
async def test_escalate_replaces_body(monkeypatch):
    async def fake_vlm(images, prompt):
        return "## Fixed\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n"

    monkeypatch.setattr(esc, "_vlm_complete", fake_vlm)
    seg = RenderedSegment(title="Fixed", level=1, path=["Fixed"],
                          page_start=0, page_end=0, markdown="broken", images=[])
    out = await esc.escalate_segment(_pdf(), seg)
    assert "| 1 | 2 |" in out
    assert out.lstrip().startswith("#")


@pytest.mark.asyncio
async def test_escalate_falls_back_on_error(monkeypatch):
    async def boom(images, prompt):
        raise RuntimeError("vlm down")

    monkeypatch.setattr(esc, "_vlm_complete", boom)
    seg = RenderedSegment(title="t", level=1, path=["t"],
                          page_start=0, page_end=0, markdown="original body", images=[])
    out = await esc.escalate_segment(_pdf(), seg)
    assert out == "original body"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_pdf_escalate_vlm.py -v`
Expected: FAIL — `module 'app.services.pdf_escalate' has no attribute 'render_pages_png'`.

- [ ] **Step 3: Implement rendering + VLM call**

Append to `backend/app/services/pdf_escalate.py`:

```python
import base64

import fitz
import httpx

from app.core.config import settings
from app.services.sanitize import sanitize_markdown

_VLM_PROMPT = (
    "You are converting one or more pages of a product documentation PDF into "
    "clean GitHub-Flavored Markdown. Reproduce the content faithfully and in "
    "reading order. Render every table as a proper Markdown table with correct "
    "rows and columns. Do not add commentary, code fences, or explanations. "
    "Output ONLY the Markdown."
)


def render_pages_png(pdf_bytes: bytes, page_start: int, page_end: int, dpi: int) -> list[bytes]:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        out: list[bytes] = []
        last = min(page_end, doc.page_count - 1)
        for pno in range(max(0, page_start), last + 1):
            pix = doc.load_page(pno).get_pixmap(dpi=dpi)
            out.append(pix.tobytes("png"))
        return out
    finally:
        doc.close()


async def _vlm_complete(images: list[bytes], prompt: str) -> str:
    if not settings.pdf_vlm_api_key:
        raise ValueError("pdf_vlm_api_key is not set")
    content = [{"type": "text", "text": prompt}]
    for img in images:
        b64 = base64.b64encode(img).decode("ascii")
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{b64}"},
        })
    body = {
        "model": settings.pdf_vlm_model,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": 4096,
    }
    headers = {
        "Authorization": f"Bearer {settings.pdf_vlm_api_key}",
        "content-type": "application/json",
    }
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(settings.pdf_vlm_base_url, headers=headers, json=body)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


def _strip_fences(text: str) -> str:
    m = re.search(r"```(?:markdown)?\s*(.*?)\s*```", text, re.DOTALL)
    return m.group(1) if m else text


async def escalate_segment(pdf_bytes: bytes, segment: RenderedSegment) -> str:
    """Re-convert one segment via the VLM. Returns the original markdown on failure."""
    try:
        images = render_pages_png(
            pdf_bytes, segment.page_start, segment.page_end, settings.pdf_vlm_dpi
        )
        if not images:
            return segment.markdown
        raw = await _vlm_complete(images, _VLM_PROMPT)
        cleaned = sanitize_markdown(_strip_fences(raw).strip())
        if not cleaned.strip():
            return segment.markdown
        if not cleaned.lstrip().startswith("#"):
            hashes = "#" * max(1, segment.level)
            cleaned = f"{hashes} {segment.title}\n\n{cleaned}"
        return cleaned
    except Exception as exc:  # noqa: BLE001 - fallback keeps the docling output
        logger.warning("VLM escalation failed for %r: %s", segment.title, exc)
        return segment.markdown
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_pdf_escalate_vlm.py -v`
Expected: PASS (all three).

- [ ] **Step 5: Commit**

```bash
git add app/services/pdf_escalate.py tests/test_pdf_escalate_vlm.py
git commit -m "feat(pdf): OpenRouter VLM escalation for flagged segments"
```

---

### Task 8: Wire the new pipeline into `run_pdf_extraction`

**Files:**
- Modify: `backend/app/services/pdf_import.py` (the conversion/segment section: imports, the `segment_pdf_async`/`convert_segments_async` usage in `run_pdf_extraction`, and remove the now-dead heuristic/LLM segmentation helpers + the page-range `_render_segment`/`render_segments`/`convert_segments_async`).
- Test: `backend/tests/test_pdf_pipeline_integration.py` (create)

**Interfaces:**
- Consumes: `convert_pdf` (Task 4), `split_into_segments` + `RenderedSegment` (Task 5), `score_segment` + `escalate_segment` (Tasks 6–7), the outline helper `_outline_segments` (kept).
- Produces: updated `run_pdf_extraction` that (1) converts once off-thread, (2) splits on headings, (3) scores + escalates flagged segments within the page budget, (4) persists via the existing `process_article_result` path. New module-level coroutine:
  - `async def build_segments(pdf_bytes: bytes, progress=None) -> list[RenderedSegment]` — `convert_pdf` (off-thread) → `_outline_segments` → `split_into_segments` → score → escalate (budgeted) → return. `progress(done, total)` awaited per escalation.

Keep: `acquire_pdf`, `Segment`, `_outline_segments`, the byte-hash fast path, the TOC-tree build, `derive_pdf_topic_key` usage, `process_article_result`/`_reconcile_removals` calls.
Remove: `heuristic_segments`, `_body_font_size`, `_llm_segment_titles`, `_titles_to_segments`, `segment_pdf`, `segment_pdf_async`, `_render_segment`, `segment_to_markdown`, `render_segments`, `convert_segments_async`, `RenderedImage` (now imported from `pdf_convert`), and the `pymupdf4llm`/`tempfile`/`_IMG_MARKER` imports they used. Update the existing PDF tests that referenced removed symbols (see Step 5 note).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_pdf_pipeline_integration.py
import os
import sys

import fitz
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.services.pdf_import as pi
from app.services.pdf_convert import ConvertedDoc


def _outline_pdf() -> bytes:
    doc = fitz.open()
    for _ in range(2):
        doc.new_page()
    doc.set_toc([[1, "Alpha Section", 1], [1, "Beta Section", 1]])
    return doc.tobytes()


@pytest.mark.asyncio
async def test_build_segments_splits_outline_without_bleed(monkeypatch):
    md = "## Alpha Section\n\nAlpha body.\n\n## Beta Section\n\nBeta body.\n"
    monkeypatch.setattr(
        pi, "convert_pdf",
        lambda b: ConvertedDoc(markdown=md, headings=[], page_texts=[md, ""],
                               table_pages=set(), images=[], engine="docling"),
    )
    # No VLM escalation in this test.
    monkeypatch.setattr(pi.settings, "pdf_vlm_escalation_enabled", False)

    segs = await pi.build_segments(_outline_pdf())
    assert [s.title for s in segs] == ["Alpha Section", "Beta Section"]
    assert "Beta" not in segs[0].markdown
    assert "Alpha body." not in segs[1].markdown


@pytest.mark.asyncio
async def test_build_segments_escalates_flagged_only(monkeypatch):
    # One ragged table (flagged) + one clean section.
    md = (
        "## Bad\n\n| a | b |\n| --- | --- |\n| 1 | 2 | 3 |\n\n"
        "## Good\n\nfine prose here.\n"
    )
    monkeypatch.setattr(
        pi, "convert_pdf",
        lambda b: ConvertedDoc(markdown=md, headings=[], page_texts=[md, ""],
                               table_pages=set(), images=[], engine="docling"),
    )
    monkeypatch.setattr(pi.settings, "pdf_vlm_escalation_enabled", True)
    monkeypatch.setattr(pi.settings, "pdf_vlm_max_pages_per_run", 30)

    calls = []

    async def fake_escalate(pdf_bytes, segment):
        calls.append(segment.title)
        return "## Bad\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n"

    monkeypatch.setattr(pi, "escalate_segment", fake_escalate)

    segs = await pi.build_segments(_outline_pdf())
    assert calls == ["Bad"]                       # only the flagged one escalated
    bad = next(s for s in segs if s.title == "Bad")
    assert "| 1 | 2 |" in bad.markdown and "| 1 | 2 | 3 |" not in bad.markdown
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_pdf_pipeline_integration.py -v`
Expected: FAIL — `module 'app.services.pdf_import' has no attribute 'build_segments'`.

- [ ] **Step 3: Implement `build_segments` and rewire**

In `backend/app/services/pdf_import.py`:

(a) Replace the conversion-related imports near the top. Remove `import pymupdf4llm`, the `_IMG_MARKER`, `tempfile`, and add:

```python
from app.services.pdf_convert import (
    ConvertedDoc, RenderedImage, RenderedSegment, convert_pdf, split_into_segments,
)
from app.services.pdf_escalate import escalate_segment, score_segment
```

(b) Keep `Segment` and `_outline_segments`. Delete the now-dead helpers listed in the Interfaces block (heuristic/LLM/page-range render functions). Add:

```python
def _outline_for(pdf_bytes: bytes) -> list[Segment]:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        return _outline_segments(doc)
    finally:
        doc.close()


async def build_segments(
    pdf_bytes: bytes,
    progress: "collections.abc.Callable[[int, int], collections.abc.Awaitable[None]] | None" = None,
) -> list[RenderedSegment]:
    """Convert the whole PDF (off the event loop), split on heading boundaries,
    then VLM-escalate only low-confidence segments within the per-run page budget."""
    converted: ConvertedDoc = await asyncio.to_thread(convert_pdf, pdf_bytes)
    outline = _outline_for(pdf_bytes)
    segments = split_into_segments(converted, outline)

    if not settings.pdf_vlm_escalation_enabled:
        return segments

    flagged = [s for s in segments if score_segment(s, converted)]
    budget = settings.pdf_vlm_max_pages_per_run
    done = 0
    total = len(flagged)
    for seg in flagged:
        pages = seg.page_end - seg.page_start + 1
        if pages > budget:
            continue
        new_md = await escalate_segment(pdf_bytes, seg)
        seg.markdown = new_md
        seg.images = [img for img in converted.images if img.filename in new_md] or seg.images
        budget -= pages
        done += 1
        if progress is not None:
            await progress(done, total)
    return segments
```

(c) In `run_pdf_extraction`, replace the segmentation + conversion block. Find the section that currently calls `segment_pdf_async` and `convert_segments_async` (around lines 393–445) and replace it so segments come from `build_segments`:

```python
    run.current_phase = "pdf_convert"
    await db.commit()

    async def _convert_progress(done: int, total: int) -> None:
        run.articles_extracted = done
        await db.commit()
        if total and (done == 1 or done % 5 == 0 or done == total):
            logger.info("PDF VLM escalation: %d/%d segments re-converted", done, total)

    rendered_segments = await build_segments(pdf_bytes, _convert_progress)
    run.articles_total = len(rendered_segments)
    await db.commit()

    await db.execute(delete(TOCEntry).where(TOCEntry.source_id == source.id))
    await db.flush()

    entry_ids: list[uuid.UUID] = []
    levels: list[int] = []
    article_inputs: list[tuple] = []
    key_counts: dict[str, int] = {}
    for i, seg in enumerate(rendered_segments):
        parent_id = None
        for j in range(i - 1, -1, -1):
            if levels[j] < seg.level:
                parent_id = entry_ids[j]
                break
        base_key = derive_pdf_topic_key(seg.path or [seg.title])
        n = key_counts.get(base_key, 0) + 1
        key_counts[base_key] = n
        topic_key = base_key if n == 1 else f"{base_key}-{n}"
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
        article_inputs.append(
            (toc.id, i, seg.title, topic_key, url, seg.markdown, seg.images)
        )
```

The remainder of `run_pdf_extraction` (the `content_scraping` phase reset, `articles_total` reccount over non-empty markdown, the `process_article_result` loop, `_reconcile_removals`, and completion) is **unchanged** — it already consumes `article_inputs` tuples of `(toc_id, sort_order, title, topic_key, url, md, images)`.

- [ ] **Step 4: Run the integration tests**

Run: `pytest tests/test_pdf_pipeline_integration.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Fix and run the rest of the PDF suite**

Removing the old helpers breaks tests that imported them. Delete/rewrite the now-obsolete tests for removed symbols and keep the still-valid ones:
- Delete: `tests/test_pdf_segment_heuristic.py`, `tests/test_pdf_segment_llm.py`, `tests/test_pdf_convert_async.py`, `tests/test_pdf_to_markdown.py`, `tests/test_pdf_renderer.py` *(only those asserting removed functions — verify each by grep before deleting)*.
- In `tests/test_pdf_segment.py`, keep `_outline_segments`-based assertions; remove any importing `segment_pdf`/`segment_to_markdown` (replaced by `split_into_segments`).

Run: `grep -rl -E "segment_pdf|segment_to_markdown|convert_segments_async|heuristic_segments|render_segments|_render_segment|_titles_to_segments" tests/`
For each hit, update or delete the test per the above.

Run: `pytest tests/ -k pdf -v`
Expected: PASS (whole PDF suite green).

- [ ] **Step 6: Commit**

```bash
git add app/services/pdf_import.py tests/
git commit -m "feat(pdf): wire docling+split+escalation pipeline into run_pdf_extraction"
```

---

### Task 9: Pre-bake Docling models into the worker image

**Files:**
- Modify: `backend/Dockerfile`

**Interfaces:** none (build-time only).

- [ ] **Step 1: Inspect the current Dockerfile**

Run: `sed -n '1,80p' Dockerfile`
Identify where `pip install -r requirements.txt` runs.

- [ ] **Step 2: Add a model pre-fetch layer**

After the `pip install -r requirements.txt` step, add a layer that downloads Docling's models at build time so the first extraction does not stall:

```dockerfile
# Pre-fetch Docling models so the first PDF extraction doesn't download at runtime.
RUN python -c "from docling.document_converter import DocumentConverter; DocumentConverter()" \
    || docling-tools models download \
    || true
```

(The `DocumentConverter()` constructor triggers model acquisition on most 2.x versions; `docling-tools models download` is the explicit fallback. `|| true` keeps the build resilient if the CLI name differs — the runtime fallback to `pymupdf` still protects us.)

- [ ] **Step 3: Build to verify**

Run: `docker build -t docextractor-backend:docling-test .`
Expected: build succeeds; the model-fetch layer runs without failing the build.

- [ ] **Step 4: Commit**

```bash
git add Dockerfile
git commit -m "build(pdf): pre-bake docling models into the worker image"
```

---

### Task 10: Validate against the real HYCU PDF

**Files:** none (manual validation; produces a short note appended to the spec).

**Interfaces:** none.

- [ ] **Step 1: Run the full pipeline on the HYCU PDF**

Run (from `backend/`, with `SCRATCH` set to the scratchpad holding `HYCU_CompatibilityMatrix.pdf`; `pdf_vlm_api_key` optionally set to exercise escalation):

```bash
SCRATCH=/tmp/claude-1000/.../scratchpad python -c "
import asyncio
import app.services.pdf_import as pi
data=open('$SCRATCH/HYCU_CompatibilityMatrix.pdf','rb').read()
segs=asyncio.run(pi.build_segments(data))
by={s.title:s for s in segs}
aos=by['Nutanix AOS'].markdown
print('Nutanix AOS contains VMware vSphere? ', 'VMware vSphere' in aos)
print('--- Nutanix AOS (first 600 chars) ---'); print(aos[:600])
print('segment count:', len(segs))
" 2>&1 | tail -40
```

Expected: `Nutanix AOS contains VMware vSphere?  False`; the AOS table renders once, intact, with consistent columns; no duplicated/mangled loose-text table.

- [ ] **Step 2: Record the result**

Append a short "Validation result" section to
`docs/superpowers/specs/2026-06-27-pdf-conversion-docling-vlm-design.md` noting
the before/after (bleed gone, table intact) and whether VLM escalation fired.

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/specs/2026-06-27-pdf-conversion-docling-vlm-design.md
git commit -m "docs(pdf): record HYCU validation result"
```

---

## Self-Review

**Spec coverage:**
- Module split (`pdf_convert`, `pdf_escalate`, slim `pdf_import`) → Tasks 4–8. ✓
- Convert-once + heading-split (no page-range bleed; tables whole) → Tasks 4–5. ✓
- No-outline path via Docling headings, removing font/LLM fallbacks → Tasks 5, 8. ✓
- Confidence scoring (ragged/missing/sparse) → Task 6. ✓
- VLM escalation via OpenRouter (Gemini default), segment granularity, page budget → Tasks 7–8. ✓
- `call_llm` image support → Task 3. ✓
- New settings → Task 1. ✓
- Docling dependency + model pre-bake → Tasks 2, 9. ✓
- Engine + escalation fallbacks (never no output) → Tasks 4, 7. ✓
- Tests (no-bleed, table-not-cut, scorer, payload shapes, fallback) → Tasks 3–8. ✓
- HYCU validation → Task 10. ✓
- Unchanged DB/diff/TOC path → Task 8 preserves `process_article_result`/`_reconcile_removals`. ✓

**Placeholder scan:** No TBD/TODO; every code step shows full code; the only "verify against installed version" notes are in the Docling spike (Task 2) and are concrete probe steps, not deferred work.

**Type consistency:** `ConvertedDoc(markdown, headings, page_texts, table_pages, images, engine)` is constructed identically in Tasks 4, 5-tests, 6-tests, 8-tests. `RenderedSegment(title, level, path, page_start, page_end, markdown, images)` consistent across Tasks 5–8. `RenderedImage(filename, data, alt)` defined once in Task 4 and imported elsewhere. `convert_pdf`/`split_into_segments`/`score_segment`/`escalate_segment`/`build_segments` signatures match across producer and consumer tasks.
