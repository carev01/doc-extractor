# Robust PDF Conversion (docling-serve + heading-split + VLM escalation) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the page-range PDF→Markdown pipeline with whole-document conversion via the **docling-serve** HTTP API, split on heading boundaries (never page ranges), plus automatic quality-gated VLM escalation that runs through docling-serve's VLM pipeline (pointed at OpenRouter) for the hard pages.

**Architecture:** The backend calls docling-serve (`http://docling.home.lan`, `X-Api-Key` auth) like it calls Firecrawl — no docling/torch embedded in the app image. `POST /v1/convert/source` (base64 file + nested options) returns whole-doc markdown plus a structured `DoclingDocument` (headings with `level`/`page_no`, tables with `page_no`). We split the markdown at heading boundaries, score each article, and re-convert only low-confidence segments via the same API with `pipeline=vlm` + `page_range` + `vlm_pipeline_model_api` (OpenRouter). The existing DB / diff / versioning / TOC and `process_article_result` machinery is unchanged.

**Tech Stack:** Python 3, FastAPI, SQLAlchemy async, PyMuPDF (`fitz`), `pymupdf4llm` (fallback only), httpx, pytest (sync DB style).

## Global Constraints

- Spec: `docs/superpowers/specs/2026-06-27-pdf-conversion-docling-vlm-design.md`.
- docling-serve: base URL `http://docling.home.lan`, auth header **`X-Api-Key`**, endpoint `POST /v1/convert/source`, request body `{"sources":[{"kind":"file","base64_string":<b64>,"filename":<name>}],"options":{...}}`, response `{"document":{"md_content","json_content"},"status","errors","processing_time"}` where `status` success is `"success"` (treat `"partial_success"` as success too).
- VLM is **OpenRouter via docling-serve's `vlm_pipeline_model_api`**, never a direct Claude/Anthropic call. Default model `qwen/qwen3-vl-32b-instruct`.
- `json_content` (`DoclingDocument`) shape used: `texts[]` items have `label` (`section_header`,`text`,`page_header`,`page_footer`,`list_item`,…), `text`, `level` (int, for headings), `prov[].page_no` (1-based). `tables[]` items have `prov[].page_no`.
- **Secrets:** `docling_serve_api_key` and `pdf_vlm_api_key` come from env (`DOCEXTRACTOR_…`). `backend/.env` is git-TRACKED — NEVER write keys there. Local validation exports them inline on the command.
- Settings use the `DOCEXTRACTOR_` env prefix via `pydantic-settings` (`app/core/config.py`).
- Conversion/escalation are HTTP I/O via `httpx.AsyncClient` (async) — they do NOT block the event loop, so no `asyncio.to_thread` is needed for them.
- Never regress to "no output": a docling-serve failure falls back to a whole-doc `pymupdf4llm` conversion.
- No DB schema change. `process_article_result` / `_reconcile_removals` / TOC-tree build stay as-is.
- Tests are synchronous (`psycopg2` + sync `Session`); async functions tested with `pytest.mark.asyncio`. Build deterministic PDFs with `fitz`; **mock the docling-serve HTTP client** and all network in unit tests. Test files live in `backend/tests/`, each starting with `sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))`.
- Run all backend commands from `backend/`. Use `python3` and `pytest` — there is NO `python` on PATH.
- Validation PDF: `$SCRATCH/HYCU_CompatibilityMatrix.pdf` (24 pages; outline puts several sections on the same page — the bleed reproducer). A reference copy of the docling-serve OpenAPI spec is at `$SCRATCH/docling_serve_v1.12_openapi.json`.
- Prior work already on the branch: base PDF/VLM settings were added in commit `2230e06` (Task 1 below amends them); the embedded-docling dependency was added then reverted (commit `6402afb`).

---

### Task 1: Amend settings for docling-serve

The base `pdf_*`/`pdf_vlm_*` settings already exist (commit `2230e06`). Add the docling-serve client settings and drop the now-unused `pdf_vlm_dpi` (docling-serve renders pages itself).

**Files:**
- Modify: `backend/app/core/config.py` (the PDF conversion settings block added after `pdf_max_upload_bytes`)
- Modify: `backend/tests/test_pdf_convert_settings.py`

**Interfaces:**
- Produces: `settings.docling_serve_url: str`, `settings.docling_serve_api_key: str`, `settings.docling_serve_timeout: float`. Removes `settings.pdf_vlm_dpi`.

- [ ] **Step 1: Update the test**

Replace the body of `backend/tests/test_pdf_convert_settings.py` with:

```python
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import Settings


def _settings(**env):
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
    assert s.docling_serve_url == "http://docling.home.lan"
    assert s.docling_serve_api_key == ""
    assert s.docling_serve_timeout == 600.0
    assert s.pdf_vlm_escalation_enabled is True
    assert s.pdf_vlm_base_url == "https://openrouter.ai/api/v1/chat/completions"
    assert s.pdf_vlm_api_key == ""
    assert s.pdf_vlm_model == "qwen/qwen3-vl-32b-instruct"
    assert s.pdf_vlm_max_pages_per_run == 30


def test_pdf_settings_override_from_env_kwargs():
    s = _settings(pdf_converter="pymupdf", docling_serve_url="http://x.local",
                  pdf_vlm_max_pages_per_run=5)
    assert s.pdf_converter == "pymupdf"
    assert s.docling_serve_url == "http://x.local"
    assert s.pdf_vlm_max_pages_per_run == 5


def test_pdf_vlm_dpi_removed():
    assert not hasattr(_settings(), "pdf_vlm_dpi")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_pdf_convert_settings.py -v`
Expected: FAIL — `docling_serve_url` missing / `pdf_vlm_dpi` still present.

- [ ] **Step 3: Update the settings block**

In `backend/app/core/config.py`, replace the PDF conversion settings block (the one beginning `# PDF conversion engine (Layer A)`) with:

```python
    # PDF conversion engine (Layer A) and VLM escalation (Layer B).
    # pdf_converter: "docling" (default, remote docling-serve) | "pymupdf"
    # (in-process fallback engine). docling-serve is consumed over HTTP — no
    # docling/torch is embedded in this image.
    pdf_converter: str = "docling"
    docling_serve_url: str = "http://docling.home.lan"
    docling_serve_api_key: str = ""          # X-Api-Key (env only — .env is tracked)
    docling_serve_timeout: float = 600.0     # per-request read timeout (s)
    # VLM escalation runs through docling-serve's VLM pipeline, pointed at an
    # OpenAI-compatible remote model (OpenRouter). The app forwards the endpoint,
    # bearer key, and model name in the convert request — never calls Anthropic.
    pdf_vlm_escalation_enabled: bool = True
    pdf_vlm_base_url: str = "https://openrouter.ai/api/v1/chat/completions"
    pdf_vlm_api_key: str = ""                 # Bearer key (env only)
    pdf_vlm_model: str = "qwen/qwen3-vl-32b-instruct"
    pdf_vlm_max_pages_per_run: int = 30
```

(This drops the previous `pdf_vlm_dpi` line.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_pdf_convert_settings.py -v`
Expected: PASS (three tests).

- [ ] **Step 5: Commit**

```bash
git add app/core/config.py tests/test_pdf_convert_settings.py
git commit -m "feat(pdf): docling-serve client settings; drop pdf_vlm_dpi"
```

---

### Task 2: `docling_client.py` — async HTTP client for docling-serve

**Files:**
- Create: `backend/app/services/docling_client.py`
- Test: `backend/tests/test_docling_client.py` (create)

**Interfaces:**
- Produces:
  - `class DoclingServeError(Exception)`
  - `async def convert(pdf_bytes: bytes, *, filename: str = "source.pdf", pipeline: str = "standard", page_range: tuple[int, int] | None = None, use_vlm_api: bool = False, do_ocr: bool = False, image_export_mode: str = "embedded") -> dict` — POSTs `/v1/convert/source` and returns the `document` dict (`{"md_content", "json_content", ...}`). Raises `DoclingServeError` on transport error, non-200, error status, or missing document.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_docling_client.py
import base64
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.services.docling_client as dc


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError("err", request=None, response=None)

    def json(self):
        return self._payload


class _Client:
    captured = {}

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        _Client.captured = {"url": url, "headers": headers, "json": json}
        return _Resp({"status": "success",
                      "document": {"md_content": "# X", "json_content": {"texts": []}}})


@pytest.mark.asyncio
async def test_convert_posts_expected_request(monkeypatch):
    monkeypatch.setattr(dc.settings, "docling_serve_url", "http://docling.test")
    monkeypatch.setattr(dc.settings, "docling_serve_api_key", "secret")
    monkeypatch.setattr(dc.settings, "pdf_vlm_base_url", "http://router/v1/chat")
    monkeypatch.setattr(dc.settings, "pdf_vlm_api_key", "ork")
    monkeypatch.setattr(dc.settings, "pdf_vlm_model", "qwen/qwen3-vl-32b-instruct")
    monkeypatch.setattr(dc.httpx, "AsyncClient", _Client)

    doc = await dc.convert(b"%PDF-1.4 fake", pipeline="vlm", page_range=(2, 3),
                           use_vlm_api=True)
    assert doc["md_content"] == "# X"

    cap = _Client.captured
    assert cap["url"] == "http://docling.test/v1/convert/source"
    assert cap["headers"]["X-Api-Key"] == "secret"
    body = cap["json"]
    src = body["sources"][0]
    assert src["kind"] == "file"
    assert base64.b64decode(src["base64_string"]) == b"%PDF-1.4 fake"
    opts = body["options"]
    assert opts["to_formats"] == ["md", "json"]
    assert opts["pipeline"] == "vlm"
    assert opts["page_range"] == [2, 3]
    assert opts["vlm_pipeline_model_api"]["url"] == "http://router/v1/chat"
    assert opts["vlm_pipeline_model_api"]["headers"]["Authorization"] == "Bearer ork"
    assert opts["vlm_pipeline_model_api"]["params"]["model"] == "qwen/qwen3-vl-32b-instruct"


@pytest.mark.asyncio
async def test_convert_raises_on_error_status(monkeypatch):
    class _ErrClient(_Client):
        async def post(self, url, headers=None, json=None):
            return _Resp({"status": "failure", "errors": ["boom"], "document": None})

    monkeypatch.setattr(dc.settings, "docling_serve_url", "http://docling.test")
    monkeypatch.setattr(dc.settings, "docling_serve_api_key", "secret")
    monkeypatch.setattr(dc.httpx, "AsyncClient", _ErrClient)

    with pytest.raises(dc.DoclingServeError):
        await dc.convert(b"x")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_docling_client.py -v`
Expected: FAIL — `No module named app.services.docling_client`.

- [ ] **Step 3: Implement the client**

```python
# backend/app/services/docling_client.py
"""Thin async client for the docling-serve REST API (PDF→markdown+structure).

docling-serve runs on the homelab k3s; we consume it over HTTP exactly like
Firecrawl, so no docling/torch dependency is embedded in this image."""
from __future__ import annotations

import base64
import logging

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

_VLM_PROMPT = (
    "Convert this page to markdown. Render every table as a proper Markdown "
    "table with correct rows and columns. Do not miss any text and only output "
    "the bare markdown!"
)


class DoclingServeError(Exception):
    """Raised when docling-serve cannot convert a document."""


def _vlm_model_api() -> dict:
    return {
        "url": settings.pdf_vlm_base_url,
        "headers": {"Authorization": f"Bearer {settings.pdf_vlm_api_key}"},
        "params": {"model": settings.pdf_vlm_model},
        "prompt": _VLM_PROMPT,
    }


async def convert(
    pdf_bytes: bytes,
    *,
    filename: str = "source.pdf",
    pipeline: str = "standard",
    page_range: "tuple[int, int] | None" = None,
    use_vlm_api: bool = False,
    do_ocr: bool = False,
    image_export_mode: str = "embedded",
) -> dict:
    """POST a PDF to docling-serve /v1/convert/source; return the `document` dict
    (`md_content`, `json_content`). Raise DoclingServeError on any failure."""
    options: dict = {
        "to_formats": ["md", "json"],
        "do_ocr": do_ocr,
        "image_export_mode": image_export_mode,
        "table_mode": "accurate",
        "pipeline": pipeline,
    }
    if page_range is not None:
        options["page_range"] = [page_range[0], page_range[1]]
    if use_vlm_api:
        options["vlm_pipeline_model_api"] = _vlm_model_api()

    body = {
        "sources": [{
            "kind": "file",
            "base64_string": base64.b64encode(pdf_bytes).decode("ascii"),
            "filename": filename,
        }],
        "options": options,
    }
    url = settings.docling_serve_url.rstrip("/") + "/v1/convert/source"
    headers = {"X-Api-Key": settings.docling_serve_api_key,
               "content-type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=settings.docling_serve_timeout) as client:
            resp = await client.post(url, headers=headers, json=body)
            resp.raise_for_status()
            payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise DoclingServeError(f"docling-serve request failed: {exc}") from exc

    if payload.get("status") not in ("success", "partial_success"):
        raise DoclingServeError(
            f"docling-serve status={payload.get('status')!r} "
            f"errors={payload.get('errors')}"
        )
    doc = payload.get("document")
    if not doc:
        raise DoclingServeError("docling-serve returned no document")
    return doc
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_docling_client.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
git add app/services/docling_client.py tests/test_docling_client.py
git commit -m "feat(pdf): async docling-serve HTTP client"
```

---

### Task 3: `pdf_convert.convert_pdf` — parse docling-serve output + pymupdf fallback

**Files:**
- Create: `backend/app/services/pdf_convert.py`
- Test: `backend/tests/test_pdf_convert.py` (create)

**Interfaces:**
- Consumes: `docling_client.convert` / `DoclingServeError` (Task 2).
- Produces:
  - `@dataclass RenderedImage(filename: str, data: bytes, alt: str)`
  - `@dataclass DocHeading(text: str, level: int, page0: int)`
  - `@dataclass ConvertedDoc(markdown: str, headings: list[DocHeading], page_texts: list[str], table_pages: set[int], images: list[RenderedImage], engine: str)`
  - `async def convert_pdf(pdf_bytes: bytes) -> ConvertedDoc` — docling-serve standard conversion; on `DoclingServeError`/empty, falls back to `pymupdf4llm` whole-doc.
  - `def _content_address_data_uris(markdown: str) -> tuple[str, list[RenderedImage]]` — rewrite embedded `data:image/...;base64,…` markers to content-addressed `<sha16>.png`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_pdf_convert.py
import base64
import os
import sys

import fitz
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.services.pdf_convert as pc
import app.services.docling_client as dc


def _pdf() -> bytes:
    doc = fitz.open()
    p = doc.new_page()
    p.insert_text((72, 72), "Alpha body content here.")
    return doc.tobytes()


@pytest.mark.asyncio
async def test_convert_pdf_parses_docling_response(monkeypatch):
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 16
    data_uri = "data:image/png;base64," + base64.b64encode(png).decode()
    md = f"# Alpha\n\n![pic]({data_uri})\n"
    json_content = {
        "texts": [
            {"label": "section_header", "text": "Alpha", "level": 1,
             "prov": [{"page_no": 1}]},
            {"label": "page_footer", "text": "HYCU | 1", "prov": [{"page_no": 1}]},
        ],
        "tables": [{"prov": [{"page_no": 1}]}],
    }

    async def fake_convert(pdf_bytes, **kw):
        return {"md_content": md, "json_content": json_content}

    monkeypatch.setattr(pc.docling_client, "convert", fake_convert)
    monkeypatch.setattr(pc.settings, "pdf_converter", "docling")

    out = await pc.convert_pdf(_pdf())
    assert out.engine == "docling"
    assert [h.text for h in out.headings] == ["Alpha"]
    assert out.headings[0].level == 1 and out.headings[0].page0 == 0
    assert out.table_pages == {0}
    assert len(out.images) == 1 and out.images[0].filename.endswith(".png")
    assert out.images[0].filename in out.markdown
    assert "data:image/png" not in out.markdown


@pytest.mark.asyncio
async def test_convert_pdf_falls_back_to_pymupdf(monkeypatch):
    async def boom(pdf_bytes, **kw):
        raise dc.DoclingServeError("down")

    monkeypatch.setattr(pc.docling_client, "convert", boom)
    monkeypatch.setattr(pc.settings, "pdf_converter", "docling")

    out = await pc.convert_pdf(_pdf())
    assert out.engine == "pymupdf"
    assert "Alpha" in out.markdown
    assert len(out.page_texts) == 1


def test_content_address_data_uris():
    png = b"\x89PNG\r\n\x1a\n" + b"1" * 16
    uri = "data:image/png;base64," + base64.b64encode(png).decode()
    md = f"x ![cat]({uri}) y"
    new_md, images = pc._content_address_data_uris(md)
    assert len(images) == 1
    assert images[0].data == png
    assert images[0].filename in new_md
    assert "base64" not in new_md
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_pdf_convert.py -v`
Expected: FAIL — `No module named app.services.pdf_convert`.

- [ ] **Step 3: Implement the module**

```python
# backend/app/services/pdf_convert.py
"""Whole-document PDF→markdown conversion via docling-serve, with a pymupdf
fallback, plus heading-boundary splitting into article segments.

Converting the whole document at once preserves reading order and keeps tables
whole across page breaks; splitting happens later at heading boundaries (never
page ranges), which eliminates the cross-section bleed of the old page-range
pipeline."""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field

import fitz  # PyMuPDF
import pymupdf4llm

from app.core.config import settings
from app.services import docling_client
from app.services.docling_client import DoclingServeError
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


# ── image content-addressing ────────────────────────────────────────────────

_DATA_URI_RE = re.compile(
    r"!\[(?P<alt>[^\]]*)\]\(data:image/[A-Za-z0-9.+-]+;base64,(?P<b64>[A-Za-z0-9+/=\s]+)\)"
)
_IMG_MARKER = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<target>[^)]+)\)")


def _content_address_data_uris(markdown: str) -> tuple[str, list[RenderedImage]]:
    """Rewrite embedded data-URI image markers to content-addressed <sha>.png."""
    images: list[RenderedImage] = []
    seen: set[str] = set()

    def _replace(m: "re.Match") -> str:
        b64 = "".join(m.group("b64").split())
        try:
            data = base64.b64decode(b64)
        except Exception:  # noqa: BLE001 - leave malformed URIs untouched
            return m.group(0)
        sha = hashlib.sha256(data).hexdigest()[:16]
        filename = f"{sha}.png"
        if sha not in seen:
            seen.add(sha)
            images.append(RenderedImage(filename=filename, data=data, alt=m.group("alt")))
        return f"![{m.group('alt')}]({filename})"

    return _DATA_URI_RE.sub(_replace, markdown), images


def _content_address_files(markdown: str, image_dir: str) -> tuple[str, list[RenderedImage]]:
    """Rewrite file-path image markers (pymupdf4llm fallback) to <sha>.png."""
    images: list[RenderedImage] = []
    seen: dict[str, str] = {}
    seen_shas: set[str] = set()

    def _replace(m: "re.Match") -> str:
        target = m.group("target")
        alt = m.group("alt")
        if target.startswith("data:"):
            return m.group(0)
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


# ── conversion ──────────────────────────────────────────────────────────────

def _page_texts(pdf_bytes: bytes) -> list[str]:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        return [page.get_text("text") for page in doc]
    finally:
        doc.close()


def _parse_headings(json_content: dict) -> list[DocHeading]:
    out: list[DocHeading] = []
    for item in (json_content.get("texts") or []):
        if item.get("label") not in ("section_header", "title"):
            continue
        text = (item.get("text") or "").strip()
        if not text:
            continue
        prov = item.get("prov") or []
        page0 = (prov[0].get("page_no", 1) - 1) if prov else 0
        level = 1 if item.get("label") == "title" else int(item.get("level") or 1)
        out.append(DocHeading(text=text, level=level, page0=page0))
    return out


def _parse_table_pages(json_content: dict) -> set[int]:
    pages: set[int] = set()
    for t in (json_content.get("tables") or []):
        prov = t.get("prov") or []
        if prov:
            pages.add(prov[0].get("page_no", 1) - 1)
    return pages


def _convert_pymupdf(pdf_bytes: bytes) -> ConvertedDoc:
    """Whole-doc pymupdf4llm conversion (no page ranges → no boundary bleed)."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        with tempfile.TemporaryDirectory() as image_dir:
            md = pymupdf4llm.to_markdown(
                doc, write_images=True, image_path=image_dir, image_format="png"
            ) or ""
            md, images = _content_address_files(md, image_dir)
    finally:
        doc.close()
    return ConvertedDoc(
        markdown=sanitize_markdown(md), headings=[], page_texts=_page_texts(pdf_bytes),
        table_pages=set(), images=images, engine="pymupdf",
    )


async def convert_pdf(pdf_bytes: bytes) -> ConvertedDoc:
    """Convert a whole PDF to markdown. docling-serve first; pymupdf on failure."""
    if settings.pdf_converter == "pymupdf":
        return _convert_pymupdf(pdf_bytes)
    try:
        doc = await docling_client.convert(
            pdf_bytes, pipeline="standard", image_export_mode="embedded"
        )
        md = doc.get("md_content") or ""
        if not md.strip():
            raise DoclingServeError("empty markdown")
        json_content = doc.get("json_content") or {}
        md, images = _content_address_data_uris(md)
        return ConvertedDoc(
            markdown=sanitize_markdown(md),
            headings=_parse_headings(json_content),
            page_texts=_page_texts(pdf_bytes),
            table_pages=_parse_table_pages(json_content),
            images=images,
            engine="docling",
        )
    except DoclingServeError as exc:
        logger.warning("docling-serve failed (%s); falling back to pymupdf", exc)
        return _convert_pymupdf(pdf_bytes)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_pdf_convert.py -v`
Expected: PASS (three tests).

- [ ] **Step 5: Commit**

```bash
git add app/services/pdf_convert.py tests/test_pdf_convert.py
git commit -m "feat(pdf): convert_pdf via docling-serve with pymupdf fallback"
```

---

### Task 4: `split_into_segments` — heading-split (no page-range bleed)

**Files:**
- Modify: `backend/app/services/pdf_convert.py` (append)
- Test: `backend/tests/test_pdf_split.py` (create)

**Interfaces:**
- Consumes: `ConvertedDoc`, `DocHeading`, `RenderedImage` (Task 3); `Segment` (the outline dataclass from `pdf_import` — `title, level, page_start, page_end, path`).
- Produces:
  - `@dataclass RenderedSegment(title: str, level: int, path: list[str], page_start: int, page_end: int, markdown: str, images: list[RenderedImage])`
  - `def split_into_segments(converted: ConvertedDoc, outline: list[Segment]) -> list[RenderedSegment]` — split `converted.markdown` at heading boundaries. If `outline` is non-empty, use the outline titles as boundaries (locate each as a markdown heading line); else use `converted.headings`; if neither yields a boundary, split on every ATX heading; if still none, one whole-doc segment titled "Document".

Splitting is by **markdown line offset**, so a table is never cut and adjacent sections never bleed.

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
    md = "## Alpha Section\n\nAlpha body.\n\n## Beta Section\n\nBeta body.\n"
    outline = [
        Segment(title="Alpha Section", level=1, page_start=0, page_end=0, path=["Alpha Section"]),
        Segment(title="Beta Section", level=1, page_start=0, page_end=0, path=["Beta Section"]),
    ]
    segs = split_into_segments(_doc(md), outline)
    assert [s.title for s in segs] == ["Alpha Section", "Beta Section"]
    assert "Alpha body." in segs[0].markdown
    assert "Beta" not in segs[0].markdown
    assert "Beta body." in segs[1].markdown
    assert "Alpha body." not in segs[1].markdown


def test_split_never_cuts_a_table():
    md = ("## One\n\nintro\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n| 3 | 4 |\n\n"
          "## Two\n\ntail\n")
    outline = [
        Segment(title="One", level=1, page_start=0, page_end=0, path=["One"]),
        Segment(title="Two", level=1, page_start=0, page_end=0, path=["Two"]),
    ]
    segs = split_into_segments(_doc(md), outline)
    one = next(s for s in segs if s.title == "One").markdown
    assert "| 1 | 2 |" in one and "| 3 | 4 |" in one


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

Append to `backend/app/services/pdf_convert.py`. Note: `Segment` (from
`pdf_import`) is imported ONLY under `TYPE_CHECKING` to avoid a circular import —
`pdf_import` imports this module at its top (Task 7), and `split_into_segments`
only reads attributes off the outline items (duck-typed), so `Segment` is never
needed at runtime here. Add this near the other imports at the top of the file:

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.pdf_import import Segment
```

Then append:

```python
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
    out = []
    for i, ln in enumerate(lines):
        m = _ATX_RE.match(ln.strip())
        if m:
            out.append((i, m.group(2).strip()))
    return out


def _find_heading_line(headings: list[tuple[int, str]], title: str, start: int) -> "int | None":
    t = " ".join(title.lower().split())
    for idx, text in headings:
        if idx < start:
            continue
        h = " ".join(text.lower().split())
        if h == t or t in h or h in t:
            return idx
    return None


def split_into_segments(converted: ConvertedDoc, outline: "list[Segment]") -> list[RenderedSegment]:
    md = converted.markdown
    lines = md.split("\n")
    heading_lines = _heading_lines(lines)

    # boundary tuples: (line_index, title, level, path, page_start, page_end)
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
        for idx, text in heading_lines:
            boundaries.append((idx, text, 1, [text], 0, 0))

    if not boundaries:
        return [RenderedSegment(
            title="Document", level=1, path=[], page_start=0,
            page_end=max(0, len(converted.page_texts) - 1),
            markdown=md.strip(), images=list(converted.images),
        )]

    segs: list[RenderedSegment] = []
    for i, (line, title, level, path, p_start, p_end) in enumerate(boundaries):
        end_line = boundaries[i + 1][0] if i + 1 < len(boundaries) else len(lines)
        body = "\n".join(lines[line:end_line]).strip()
        segs.append(RenderedSegment(
            title=title, level=level, path=path,
            page_start=p_start, page_end=p_end,
            markdown=body,
            images=[img for img in converted.images if img.filename in body],
        ))
    return segs
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_pdf_split.py -v`
Expected: PASS (all four).

- [ ] **Step 5: Commit**

```bash
git add app/services/pdf_convert.py tests/test_pdf_split.py
git commit -m "feat(pdf): heading-boundary split (no page-range bleed)"
```

---

### Task 5: `pdf_escalate.score_segment` — confidence signals

**Files:**
- Create: `backend/app/services/pdf_escalate.py`
- Test: `backend/tests/test_pdf_escalate_score.py` (create)

**Interfaces:**
- Consumes: `RenderedSegment` (Task 4), `ConvertedDoc.table_pages` / `page_texts` (Task 3).
- Produces: `def score_segment(segment: RenderedSegment, converted: ConvertedDoc) -> list[str]` — issue codes `"ragged_table"`, `"missing_table"`, `"sparse_text"`; empty == confident.

Heuristics:
- `ragged_table`: a markdown table whose body rows' cell count differs from the header, or a header with no separator, or header+separator with zero body rows.
- `missing_table`: a page in `[page_start, page_end]` is in `converted.table_pages` but the segment markdown has no `|` table line.
- `sparse_text`: segment markdown length < 50% of concatenated `page_texts[page_start:page_end+1]` length, and that raw length > 200 chars.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_pdf_escalate_score.py
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.pdf_convert import ConvertedDoc, RenderedSegment
from app.services.pdf_escalate import score_segment


def _conv(page_texts, table_pages=None):
    return ConvertedDoc(markdown="", headings=[], page_texts=page_texts,
                        table_pages=table_pages or set(), images=[], engine="docling")


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
    assert "missing_table" in score_segment(_seg(md), _conv(["x" * 50], table_pages={0}))


def test_sparse_text_flagged():
    md = "## t\n\ntiny\n"
    assert "sparse_text" in score_segment(_seg(md), _conv(["y" * 1000]))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_pdf_escalate_score.py -v`
Expected: FAIL — `No module named app.services.pdf_escalate`.

- [ ] **Step 3: Implement the scorer**

```python
# backend/app/services/pdf_escalate.py
"""Confidence scoring + VLM re-conversion of low-confidence PDF segments.

The standard docling-serve conversion is good but not perfect on the hardest
tables. score_segment flags segments worth re-doing; escalate_segment re-converts
them via docling-serve's VLM pipeline (pointed at OpenRouter)."""
from __future__ import annotations

import logging
import re

from app.services.pdf_convert import ConvertedDoc, RenderedSegment

logger = logging.getLogger(__name__)

_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_SEP_RE = re.compile(r"^\s*\|?\s*:?-{3,}.*$")


def _cell_count(row: str) -> int:
    return len(row.strip().strip("|").split("|"))


def _has_ragged_table(md: str) -> bool:
    lines = md.split("\n")
    i, n = 0, len(md.split("\n"))
    while i < n:
        if _TABLE_ROW_RE.match(lines[i]):
            block = []
            while i < n and _TABLE_ROW_RE.match(lines[i]):
                block.append(lines[i])
                i += 1
            if len(block) < 2:
                return True
            header_cells = _cell_count(block[0])
            body = [b for b in block[2:] if not _SEP_RE.match(b)]
            if not body:
                return True
            if any(_cell_count(r) != header_cells for r in body):
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
        converted.page_texts[p] for p in seg_pages
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

### Task 6: `escalate_segment` — VLM re-conversion via docling-serve

**Files:**
- Modify: `backend/app/services/pdf_escalate.py` (append)
- Test: `backend/tests/test_pdf_escalate_vlm.py` (create)

**Interfaces:**
- Consumes: `RenderedSegment`; `docling_client.convert` / `DoclingServeError`.
- Produces: `async def escalate_segment(pdf_bytes: bytes, segment: RenderedSegment) -> str` — re-convert the segment's page range via docling-serve's VLM pipeline (`pipeline="vlm"`, `page_range`, `use_vlm_api=True`); return cleaned markdown (re-prepend the heading if the model dropped it). Returns `segment.markdown` unchanged on any `DoclingServeError`.

> Known v1 limitation: an escalated segment's body is replaced with VLM markdown rendered with `image_export_mode="placeholder"`, so figures inside a *flagged* segment are not re-embedded. Flagged segments are overwhelmingly table/text issues, not figures; sub-segment image retention is a future refinement.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_pdf_escalate_vlm.py
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.services.pdf_escalate as esc
import app.services.docling_client as dc
from app.services.pdf_convert import RenderedSegment


def _seg(md="broken", title="Fixed", level=1, p0=0, p1=0):
    return RenderedSegment(title=title, level=level, path=[title],
                           page_start=p0, page_end=p1, markdown=md, images=[])


@pytest.mark.asyncio
async def test_escalate_uses_vlm_pipeline_and_page_range(monkeypatch):
    captured = {}

    async def fake_convert(pdf_bytes, **kw):
        captured.update(kw)
        return {"md_content": "## Fixed\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n"}

    monkeypatch.setattr(esc.docling_client, "convert", fake_convert)
    out = await esc.escalate_segment(b"%PDF", _seg(p0=4, p1=5))
    assert captured["pipeline"] == "vlm"
    assert captured["use_vlm_api"] is True
    assert captured["page_range"] == (5, 6)        # 1-based inclusive
    assert "| 1 | 2 |" in out
    assert out.lstrip().startswith("#")


@pytest.mark.asyncio
async def test_escalate_prepends_missing_heading(monkeypatch):
    async def fake_convert(pdf_bytes, **kw):
        return {"md_content": "| a | b |\n| --- | --- |\n| 1 | 2 |\n"}

    monkeypatch.setattr(esc.docling_client, "convert", fake_convert)
    out = await esc.escalate_segment(b"%PDF", _seg(title="My Table", level=2))
    assert out.lstrip().startswith("## My Table")


@pytest.mark.asyncio
async def test_escalate_falls_back_on_error(monkeypatch):
    async def boom(pdf_bytes, **kw):
        raise dc.DoclingServeError("vlm down")

    monkeypatch.setattr(esc.docling_client, "convert", boom)
    out = await esc.escalate_segment(b"%PDF", _seg(md="original body"))
    assert out == "original body"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_pdf_escalate_vlm.py -v`
Expected: FAIL — `module 'app.services.pdf_escalate' has no attribute 'escalate_segment'`.

- [ ] **Step 3: Implement escalation**

Append to `backend/app/services/pdf_escalate.py`:

```python
from app.services import docling_client
from app.services.docling_client import DoclingServeError
from app.services.sanitize import sanitize_markdown


async def escalate_segment(pdf_bytes: bytes, segment: RenderedSegment) -> str:
    """Re-convert one segment via docling-serve's VLM pipeline (OpenRouter).
    Returns the original markdown on any docling-serve failure."""
    try:
        doc = await docling_client.convert(
            pdf_bytes,
            pipeline="vlm",
            page_range=(segment.page_start + 1, segment.page_end + 1),
            use_vlm_api=True,
            image_export_mode="placeholder",
        )
    except DoclingServeError as exc:
        logger.warning("VLM escalation failed for %r: %s", segment.title, exc)
        return segment.markdown

    cleaned = sanitize_markdown((doc.get("md_content") or "").strip())
    if not cleaned.strip():
        return segment.markdown
    if not cleaned.lstrip().startswith("#"):
        hashes = "#" * max(1, segment.level)
        cleaned = f"{hashes} {segment.title}\n\n{cleaned}"
    return cleaned
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_pdf_escalate_vlm.py -v`
Expected: PASS (all three).

- [ ] **Step 5: Commit**

```bash
git add app/services/pdf_escalate.py tests/test_pdf_escalate_vlm.py
git commit -m "feat(pdf): VLM escalation via docling-serve pipeline"
```

---

### Task 7: Wire the new pipeline into `run_pdf_extraction`

**Files:**
- Modify: `backend/app/services/pdf_import.py`
- Test: `backend/tests/test_pdf_pipeline_integration.py` (create)

**Interfaces:**
- Consumes: `convert_pdf`, `split_into_segments`, `RenderedSegment` (Tasks 3-4); `score_segment`, `escalate_segment` (Tasks 5-6); the outline helper `_outline_segments` (kept).
- Produces:
  - `async def build_segments(pdf_bytes: bytes, progress=None) -> list[RenderedSegment]` — `convert_pdf` (await, async HTTP) → `_outline_segments` → `split_into_segments` → score → escalate (budgeted by `pdf_vlm_max_pages_per_run`) → return. `progress(done, total)` awaited per escalation.
  - Updated `run_pdf_extraction` consuming `build_segments`.

Keep: `acquire_pdf`, `Segment`, `_outline_segments`, the byte-hash fast path, the TOC-tree build, `derive_pdf_topic_key` usage, `process_article_result`/`_reconcile_removals` calls.
Remove (now dead): `heuristic_segments`, `_body_font_size`, `_llm_segment_titles`, `_titles_to_segments`, `segment_pdf`, `segment_pdf_async`, `_render_segment`, `segment_to_markdown`, `render_segments`, `convert_segments_async`, the module-level `RenderedImage` (now imported from `pdf_convert`), `_IMG_MARKER`, and the `pymupdf4llm`/`tempfile` imports they used. **Keep `import fitz`, `import asyncio`, `import collections`** (still used). Update/delete the PDF tests that referenced removed symbols (Step 5).

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

    async def fake_convert(pdf_bytes):
        return ConvertedDoc(markdown=md, headings=[], page_texts=[md, ""],
                            table_pages=set(), images=[], engine="docling")

    monkeypatch.setattr(pi, "convert_pdf", fake_convert)
    monkeypatch.setattr(pi.settings, "pdf_vlm_escalation_enabled", False)

    segs = await pi.build_segments(_outline_pdf())
    assert [s.title for s in segs] == ["Alpha Section", "Beta Section"]
    assert "Beta" not in segs[0].markdown
    assert "Alpha body." not in segs[1].markdown


@pytest.mark.asyncio
async def test_build_segments_escalates_flagged_only(monkeypatch):
    md = ("## Bad\n\n| a | b |\n| --- | --- |\n| 1 | 2 | 3 |\n\n"
          "## Good\n\nfine prose here.\n")

    async def fake_convert(pdf_bytes):
        return ConvertedDoc(markdown=md, headings=[], page_texts=[md, ""],
                            table_pages=set(), images=[], engine="docling")

    monkeypatch.setattr(pi, "convert_pdf", fake_convert)
    monkeypatch.setattr(pi.settings, "pdf_vlm_escalation_enabled", True)
    monkeypatch.setattr(pi.settings, "pdf_vlm_max_pages_per_run", 30)

    calls = []

    async def fake_escalate(pdf_bytes, segment):
        calls.append(segment.title)
        return "## Bad\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n"

    monkeypatch.setattr(pi, "escalate_segment", fake_escalate)

    segs = await pi.build_segments(_outline_pdf())
    assert calls == ["Bad"]
    bad = next(s for s in segs if s.title == "Bad")
    assert "| 1 | 2 |" in bad.markdown and "| 1 | 2 | 3 |" not in bad.markdown
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_pdf_pipeline_integration.py -v`
Expected: FAIL — `module 'app.services.pdf_import' has no attribute 'build_segments'`.

- [ ] **Step 3: Rewire `pdf_import.py`**

(a) Near the top, remove `import pymupdf4llm`, `import tempfile`, the `_IMG_MARKER` constant, and the local `RenderedImage` dataclass. Keep `import asyncio`, `import collections`, `import fitz`. Add:

```python
from app.services.pdf_convert import (
    ConvertedDoc, RenderedImage, RenderedSegment, convert_pdf, split_into_segments,
)
from app.services.pdf_escalate import escalate_segment, score_segment
```

(b) Delete the dead helpers listed in Interfaces (`heuristic_segments`, `_body_font_size`, `_llm_segment_titles`, `_titles_to_segments`, `segment_pdf`, `segment_pdf_async`, `_render_segment`, `segment_to_markdown`, `render_segments`, `convert_segments_async`). Keep `Segment` and `_outline_segments`. Add:

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
    """Convert the whole PDF via docling-serve, split on heading boundaries, then
    VLM-escalate only low-confidence segments within the per-run page budget."""
    converted: ConvertedDoc = await convert_pdf(pdf_bytes)
    outline = _outline_for(pdf_bytes)
    segments = split_into_segments(converted, outline)

    if not settings.pdf_vlm_escalation_enabled:
        return segments

    flagged = [s for s in segments if score_segment(s, converted)]
    budget = settings.pdf_vlm_max_pages_per_run
    done, total = 0, len(flagged)
    for seg in flagged:
        pages = seg.page_end - seg.page_start + 1
        if pages > budget:
            continue
        new_md = await escalate_segment(pdf_bytes, seg)
        seg.markdown = new_md
        matched = [img for img in converted.images if img.filename in new_md]
        seg.images = matched or seg.images
        budget -= pages
        done += 1
        if progress is not None:
            await progress(done, total)
    return segments
```

(c) In `run_pdf_extraction`, replace the segmentation + conversion block (the part that called `segment_pdf_async` then `convert_segments_async` and built `article_inputs`) with:

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

The remainder of `run_pdf_extraction` (the `content_scraping` phase, the non-empty `articles_total` recount, the `process_article_result` loop over `article_inputs`, `_reconcile_removals`, completion) is **unchanged** — it already consumes `(toc_id, sort_order, title, topic_key, url, md, images)` tuples.

- [ ] **Step 4: Run the integration tests**

Run: `pytest tests/test_pdf_pipeline_integration.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Fix and run the rest of the PDF suite**

Removing the old helpers breaks tests importing them. Find them:

Run: `grep -rl -E "segment_pdf|segment_to_markdown|convert_segments_async|heuristic_segments|render_segments|_render_segment|_titles_to_segments|_body_font_size|_llm_segment_titles" tests/`

For each hit, delete the test file if its whole purpose was a removed helper (candidates: `test_pdf_segment.py`, `test_pdf_segment_heuristic.py`, `test_pdf_segment_llm.py`, `test_pdf_convert_async.py`, `test_pdf_to_markdown.py`, `test_pdf_renderer.py`), or update it if it also covers kept behaviour. Verify `_outline_segments` still has coverage (keep those assertions, moving them into `test_pdf_split.py`-style if needed).

Run: `pytest tests/ -k pdf -v`
Expected: PASS (whole PDF suite green).

- [ ] **Step 6: Commit**

```bash
git add app/services/pdf_import.py tests/
git commit -m "feat(pdf): wire docling-serve convert+split+escalation into run_pdf_extraction"
```

---

### Task 8: Validate against the real HYCU PDF + live docling-serve

Manual validation against the running service. The controller supplies the
docling-serve and OpenRouter keys via env (never committed).

**Files:** none (produces a "Validation result" note appended to the spec).

- [ ] **Step 1: Run the standard pipeline (no escalation) on the HYCU PDF**

Run (from `backend/`, with `$SCRATCH` = the scratchpad holding the PDF, and the docling key in env):

```bash
SCRATCH=<scratchpad> DOCEXTRACTOR_DOCLING_SERVE_API_KEY=<key> \
DOCEXTRACTOR_PDF_VLM_ESCALATION_ENABLED=false python3 -c "
import asyncio
import app.services.pdf_import as pi
data=open('$SCRATCH/HYCU_CompatibilityMatrix.pdf','rb').read()
segs=asyncio.run(pi.build_segments(data))
by={s.title:s for s in segs}
aos=by.get('Nutanix AOS')
print('segment count:', len(segs))
print('has Nutanix AOS:', aos is not None)
if aos:
    print('AOS contains \"VMware vSphere\":', 'VMware vSphere' in aos.markdown)
    print('--- AOS (first 600) ---'); print(aos.markdown[:600])
"
```

Expected: `AOS contains "VMware vSphere": False`; the AOS table renders once, intact; no duplicated/mangled loose-text table.

- [ ] **Step 2: Run one VLM escalation round-trip**

Run with escalation enabled and both keys in env:

```bash
SCRATCH=<scratchpad> DOCEXTRACTOR_DOCLING_SERVE_API_KEY=<key> \
DOCEXTRACTOR_PDF_VLM_API_KEY=<openrouter-key> python3 -c "
import asyncio, logging
logging.basicConfig(level=logging.INFO)
import app.services.pdf_import as pi
data=open('$SCRATCH/HYCU_CompatibilityMatrix.pdf','rb').read()
segs=asyncio.run(pi.build_segments(data))
print('segments:', len(segs))
"
```

Expected: completes without error; if any segment is flagged, an escalation log line appears and the run still finishes. (A clean run with no flags is also a valid pass — note which occurred.)

- [ ] **Step 3: Record the result**

Append a "Validation result" section to
`docs/superpowers/specs/2026-06-27-pdf-conversion-docling-vlm-design.md` noting
before/after (bleed gone, table intact), segment count, and whether escalation fired.

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/specs/2026-06-27-pdf-conversion-docling-vlm-design.md
git commit -m "docs(pdf): record HYCU validation result"
```

---

## Self-Review

**Spec coverage:**
- docling-serve client (X-Api-Key, /v1/convert/source, base64 source) → Task 2. ✓
- Convert-once + parse json_content (headings/tables/images) + pymupdf fallback → Task 3. ✓
- Heading-split, no page-range bleed, tables whole, no-outline via docling headings → Task 4. ✓
- Confidence scoring → Task 5. ✓
- VLM escalation via docling-serve VLM pipeline + page_range + OpenRouter forwarding → Tasks 2 (`use_vlm_api`), 6. ✓
- Settings incl. docling_serve_* and OpenRouter forwarding; pdf_vlm_dpi dropped → Task 1. ✓
- Async HTTP (no to_thread); never-no-output fallback → Tasks 3, 7. ✓
- Secrets via env, never in tracked .env → Global Constraints, Task 8. ✓
- Embedded docling removed → done pre-plan (commit 6402afb). ✓
- Unchanged DB/diff/TOC path → Task 7 preserves process_article_result/_reconcile_removals. ✓
- HYCU validation incl. one VLM round-trip → Task 8. ✓

**Placeholder scan:** No TBD/TODO; every code step has full code; the one "known v1 limitation" (escalated-segment images) is an explicit, bounded scope decision, not deferred work.

**Type consistency:** `ConvertedDoc(markdown, headings, page_texts, table_pages, images, engine)` constructed identically across Tasks 3-7. `RenderedSegment(title, level, path, page_start, page_end, markdown, images)` consistent Tasks 4-7. `RenderedImage(filename, data, alt)` defined in Task 3, imported elsewhere. `docling_client.convert(...)` kwargs (`pipeline`, `page_range`, `use_vlm_api`, `image_export_mode`) match between Task 2 (definition), Task 3 (standard call), and Task 6 (vlm call). `convert_pdf` is async in Tasks 3 and 7. `escalate_segment(pdf_bytes, segment)` async, consistent Tasks 6-7.
