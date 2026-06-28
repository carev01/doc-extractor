# PDF Pipeline Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix event-loop starvation that reaps long PDF conversions, add real progress/log monitoring, and stop the segment-drop that wrongly removes articles — all in one PR.

**Architecture:** Run the heavy synchronous conversion work off the event loop (`asyncio.to_thread`) so the worker heartbeat survives; drive the docling-serve conversion through its async submit/poll/result endpoints with a liveness callback; report meaningful phases + an up-front denominator + useful logs; and make outline→heading matching robust with a page-provenance fallback so no outline entry is ever silently dropped.

**Tech Stack:** Python 3, FastAPI, SQLAlchemy async, httpx, PyMuPDF (`fitz`), React/TS, pytest.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-06-28-pdf-pipeline-reliability-design.md`.
- docling-serve async API (v1.12.0): `POST /v1/convert/source/async` → `TaskStatusResponse` (`task_id`, `task_status`, `task_position`, `task_meta`); `GET /v1/status/poll/{task_id}` → `TaskStatusResponse`; `GET /v1/result/{task_id}` → `ConvertDocumentResponse` (`{document:{md_content,json_content}, status, errors}`). Auth header `X-Api-Key`. Terminal `task_status`: `"success"` / `"failure"` (keep polling otherwise). Verify the exact terminal strings live during Task 2.
- Heavy synchronous work (JSON parse, image content-addressing, `fitz` text/render, pymupdf fallback) MUST run via `asyncio.to_thread` — the worker `_heartbeat`/`_flush_logs` tasks (15 s / 10 s) starve if the loop blocks, and `reap_stale_runs` recycles at `stale_seconds=300`.
- Conversion fallback: any docling-serve error/timeout → `_convert_pymupdf` (never "no output").
- Run logs: any `logger.info(...)` during a run is captured to `run.log_text` by the worker's `_RunLogHandler` — emit useful lines, don't build a separate log path.
- Never silently drop an outline entry in `split_into_segments`.
- Secrets are env-only; `backend/.env` is git-tracked — never write keys there.
- Tests: sync DB style; async funcs use `pytest.mark.asyncio`; mock the docling client / httpx and build PDFs with `fitz`. Run from `backend/`. Use `python3` (no `python` on PATH).

---

### Task 1: Add `docling_serve_poll_interval` setting

**Files:**
- Modify: `backend/app/core/config.py` (docling block, after `docling_serve_timeout`)
- Test: `backend/tests/test_pdf_convert_settings.py`

**Interfaces:**
- Produces: `settings.docling_serve_poll_interval: float` (default `3.0`).

- [ ] **Step 1: Extend the settings test**

Add to `backend/tests/test_pdf_convert_settings.py` inside `test_pdf_converter_defaults`, after the `docling_serve_timeout` assertion:

```python
    assert s.docling_serve_poll_interval == 3.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_pdf_convert_settings.py::test_pdf_converter_defaults -v`
Expected: FAIL — no attribute `docling_serve_poll_interval`.

- [ ] **Step 3: Add the setting**

In `backend/app/core/config.py`, immediately after the `docling_serve_timeout: float = 600.0 ...` line:

```python
    docling_serve_poll_interval: float = 3.0  # async convert: status poll cadence (s)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_pdf_convert_settings.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/core/config.py tests/test_pdf_convert_settings.py
git commit -m "feat(pdf): add docling_serve_poll_interval setting"
```

---

### Task 2: `docling_client.convert_async` (submit/poll/result, threaded parse, page-break)

**Files:**
- Modify: `backend/app/services/docling_client.py`
- Test: `backend/tests/test_docling_client_async.py` (create)

**Interfaces:**
- Consumes: `settings.docling_serve_*`.
- Produces:
  - `async def convert_async(pdf_bytes, *, filename="source.pdf", page_range=None, use_vlm_api=False, do_ocr=False, image_export_mode="embedded", page_break_placeholder="", on_poll=None) -> dict` — POSTs `/v1/convert/source/async`, polls `/v1/status/poll/{task_id}` every `settings.docling_serve_poll_interval` (calling `await on_poll(status_dict)` each tick if given), fetches `/v1/result/{task_id}` on success, returns the `document` dict. Raises `DoclingServeError` on failure/timeout. JSON parsing of submit/result responses runs via `asyncio.to_thread`.
  - `_PAGE_BREAK = "<!-- docling-page-break -->"` module constant.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_docling_client_async.py
import base64, os, sys
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import app.services.docling_client as dc


class _Resp:
    def __init__(self, payload): self._p = payload
    def raise_for_status(self): pass
    def json(self): return self._p


class _SeqClient:
    """Returns submit→poll(started)→poll(success)→result in order."""
    seq = []
    posts = []

    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    async def post(self, url, headers=None, json=None):
        _SeqClient.posts.append((url, json))
        return _Resp({"task_id": "T1", "task_status": "pending"})

    async def get(self, url, headers=None):
        return _Resp(_SeqClient.seq.pop(0))


@pytest.mark.asyncio
async def test_convert_async_polls_then_returns_document(monkeypatch):
    monkeypatch.setattr(dc.settings, "docling_serve_url", "http://d.test")
    monkeypatch.setattr(dc.settings, "docling_serve_api_key", "k")
    monkeypatch.setattr(dc.settings, "docling_serve_poll_interval", 0.0)
    _SeqClient.seq = [
        {"task_id": "T1", "task_status": "started", "task_position": 0},
        {"task_id": "T1", "task_status": "success"},
        {"status": "success", "document": {"md_content": "# X", "json_content": {}}},
    ]
    _SeqClient.posts = []
    monkeypatch.setattr(dc.httpx, "AsyncClient", _SeqClient)

    polls = []
    async def on_poll(s): polls.append(s["task_status"])

    doc = await dc.convert_async(b"%PDF", page_break_placeholder=dc._PAGE_BREAK, on_poll=on_poll)
    assert doc["md_content"] == "# X"
    assert polls == ["started", "success"]
    # submit body carried the page-break placeholder option
    _, body = _SeqClient.posts[0]
    assert body["options"]["md_page_break_placeholder"] == dc._PAGE_BREAK
    assert _SeqClient.posts[0][0].endswith("/v1/convert/source/async")


@pytest.mark.asyncio
async def test_convert_async_raises_on_failure(monkeypatch):
    monkeypatch.setattr(dc.settings, "docling_serve_url", "http://d.test")
    monkeypatch.setattr(dc.settings, "docling_serve_api_key", "k")
    monkeypatch.setattr(dc.settings, "docling_serve_poll_interval", 0.0)
    _SeqClient.seq = [{"task_id": "T1", "task_status": "failure"}]
    monkeypatch.setattr(dc.httpx, "AsyncClient", _SeqClient)
    with pytest.raises(dc.DoclingServeError):
        await dc.convert_async(b"%PDF")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_docling_client_async.py -v`
Expected: FAIL — `convert_async` / `_PAGE_BREAK` not defined.

- [ ] **Step 3: Implement `convert_async`**

In `backend/app/services/docling_client.py`, add near the top (after imports): `import asyncio` and `import time`, and the constant:

```python
_PAGE_BREAK = "<!-- docling-page-break -->"
_TERMINAL_OK = "success"
_TERMINAL_FAIL = "failure"
```

Then add (after the existing `convert`):

```python
def _build_options(*, page_range, use_vlm_api, do_ocr, image_export_mode,
                   page_break_placeholder) -> dict:
    options: dict = {
        "to_formats": ["md", "json"],
        "do_ocr": do_ocr,
        "image_export_mode": image_export_mode,
        "table_mode": "accurate",
        "pipeline": "vlm" if use_vlm_api else "standard",
    }
    if page_range is not None:
        options["page_range"] = [page_range[0], page_range[1]]
    if page_break_placeholder:
        options["md_page_break_placeholder"] = page_break_placeholder
    if use_vlm_api:
        options["vlm_pipeline_model_api"] = _vlm_model_api()
    return options


async def convert_async(
    pdf_bytes: bytes,
    *,
    filename: str = "source.pdf",
    page_range: "tuple[int, int] | None" = None,
    use_vlm_api: bool = False,
    do_ocr: bool = False,
    image_export_mode: str = "embedded",
    page_break_placeholder: str = "",
    on_poll=None,
) -> dict:
    """Submit a convert task, poll to completion (calling on_poll each tick),
    then fetch and return the `document` dict. Raises DoclingServeError."""
    body = {
        "sources": [{
            "kind": "file",
            "base64_string": base64.b64encode(pdf_bytes).decode("ascii"),
            "filename": filename,
        }],
        "options": _build_options(
            page_range=page_range, use_vlm_api=use_vlm_api, do_ocr=do_ocr,
            image_export_mode=image_export_mode,
            page_break_placeholder=page_break_placeholder,
        ),
    }
    base = settings.docling_serve_url.rstrip("/")
    headers = {"X-Api-Key": settings.docling_serve_api_key,
               "content-type": "application/json"}
    deadline = time.monotonic() + settings.docling_serve_timeout
    try:
        async with httpx.AsyncClient(timeout=settings.docling_serve_timeout) as client:
            resp = await client.post(base + "/v1/convert/source/async",
                                     headers=headers, json=body)
            resp.raise_for_status()
            status = await asyncio.to_thread(resp.json)
            task_id = status.get("task_id")
            if not task_id:
                raise DoclingServeError("async submit returned no task_id")

            while status.get("task_status") not in (_TERMINAL_OK, _TERMINAL_FAIL):
                if time.monotonic() > deadline:
                    raise DoclingServeError("docling-serve conversion timed out")
                await asyncio.sleep(settings.docling_serve_poll_interval)
                r = await client.get(base + f"/v1/status/poll/{task_id}", headers=headers)
                r.raise_for_status()
                status = await asyncio.to_thread(r.json)
                if on_poll is not None:
                    try:
                        await on_poll(status)
                    except Exception:  # noqa: BLE001 - progress must never crash a run
                        logger.exception("on_poll callback failed")

            if status.get("task_status") == _TERMINAL_FAIL:
                raise DoclingServeError(f"docling-serve task failed: {status}")

            rr = await client.get(base + f"/v1/result/{task_id}", headers=headers)
            rr.raise_for_status()
            payload = await asyncio.to_thread(rr.json)
    except (httpx.HTTPError, ValueError) as exc:
        raise DoclingServeError(f"docling-serve async request failed: {exc}") from exc

    if payload.get("status") not in ("success", "partial_success"):
        raise DoclingServeError(
            f"docling-serve status={payload.get('status')!r} errors={payload.get('errors')}"
        )
    doc = payload.get("document")
    if not doc:
        raise DoclingServeError("docling-serve returned no document")
    return doc
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_docling_client_async.py tests/test_docling_client.py -v`
Expected: PASS (new + existing client tests).

- [ ] **Step 5: Verify terminal status strings live (quick probe)**

Run a one-off against the live service on a tiny PDF to confirm `task_status` reaches `"success"` (adjust `_TERMINAL_OK`/`_TERMINAL_FAIL` if the live values differ):

```bash
DOCEXTRACTOR_DOCLING_SERVE_API_KEY=<key> python3 -c "
import asyncio, app.services.docling_client as dc, fitz
d=fitz.open(); d.new_page().insert_text((72,72),'hi')
doc=asyncio.run(dc.convert_async(d.tobytes()))
print('ok md len', len(doc.get('md_content') or ''))
"
```
Expected: prints a non-zero md length. If it hangs or errors on status strings, correct the terminal constants and re-run Step 4.

- [ ] **Step 6: Commit**

```bash
git add app/services/docling_client.py tests/test_docling_client_async.py
git commit -m "feat(pdf): docling-serve async convert with polling + threaded parse"
```

---

### Task 3: `convert_pdf` — async + off-loop transform + page-offset map

**Files:**
- Modify: `backend/app/services/pdf_convert.py`
- Test: `backend/tests/test_pdf_convert.py`

**Interfaces:**
- Consumes: `docling_client.convert_async` / `_PAGE_BREAK` (Task 2).
- Produces:
  - `ConvertedDoc` gains `page_line_starts: list[int]` (markdown line index where each 0-based page begins; empty if unknown).
  - `async def convert_pdf(pdf_bytes, on_poll=None) -> ConvertedDoc` — uses `convert_async` (requesting the page-break placeholder), runs the whole response→`ConvertedDoc` transform and the pymupdf fallback via `asyncio.to_thread`.
  - `def _build_converted_doc(doc: dict, pdf_bytes: bytes) -> ConvertedDoc` — synchronous transform (md extract → strip page breaks + compute `page_line_starts` → content-address → parse headings/tables → page_texts).

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/test_pdf_convert.py`:

```python
@pytest.mark.asyncio
async def test_convert_pdf_uses_async_and_builds_page_line_starts(monkeypatch):
    from app.services import docling_client as dc
    md = (f"# A\n\nalpha\n\n{dc._PAGE_BREAK}\n\n# B\n\nbeta\n")
    json_content = {"texts": [{"label": "section_header", "text": "A", "level": 1,
                               "prov": [{"page_no": 1}]}], "tables": []}

    async def fake_convert_async(pdf_bytes, **kw):
        assert kw.get("page_break_placeholder") == dc._PAGE_BREAK
        return {"md_content": md, "json_content": json_content}

    monkeypatch.setattr(pc.docling_client, "convert_async", fake_convert_async)
    monkeypatch.setattr(pc.settings, "pdf_converter", "docling")

    out = await pc.convert_pdf(_pdf())
    assert out.engine == "docling"
    assert dc._PAGE_BREAK not in out.markdown          # placeholder stripped
    assert out.page_line_starts and out.page_line_starts[0] == 0
    assert len(out.page_line_starts) == 2              # two pages
    # page 2 starts at the '# B' line
    assert out.markdown.split("\n")[out.page_line_starts[1]].strip() == "# B"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_pdf_convert.py::test_convert_pdf_uses_async_and_builds_page_line_starts -v`
Expected: FAIL (`page_line_starts` missing / placeholder not stripped).

- [ ] **Step 3: Implement**

In `backend/app/services/pdf_convert.py`:

(a) Add to imports: `import asyncio` and `from app.services.docling_client import _PAGE_BREAK`.

(b) Add `page_line_starts` to `ConvertedDoc` (default empty):

```python
    page_line_starts: list[int] = field(default_factory=list)
```

(c) Add a helper to strip placeholders + compute page offsets:

```python
def _split_page_breaks(markdown: str) -> tuple[str, list[int]]:
    """Remove page-break placeholder lines; return (clean_md, page_line_starts)
    where page_line_starts[p] is the clean-markdown line index of page p (0-based)."""
    out: list[str] = []
    starts: list[int] = [0]
    for ln in markdown.split("\n"):
        if ln.strip() == _PAGE_BREAK:
            starts.append(len(out))
        else:
            out.append(ln)
    return "\n".join(out), starts
```

(d) Add `_build_converted_doc` and rewrite `convert_pdf`:

```python
def _build_converted_doc(doc: dict, pdf_bytes: bytes) -> ConvertedDoc:
    md = doc.get("md_content") or ""
    json_content = doc.get("json_content") or {}
    md, page_line_starts = _split_page_breaks(md)
    md, images = _content_address_data_uris(md)
    return ConvertedDoc(
        markdown=sanitize_markdown(md),
        headings=_parse_headings(json_content),
        page_texts=_page_texts(pdf_bytes),
        table_pages=_parse_table_pages(json_content),
        images=images,
        engine="docling",
        page_line_starts=page_line_starts,
    )


async def convert_pdf(pdf_bytes: bytes, on_poll=None) -> ConvertedDoc:
    """Convert a whole PDF to markdown via docling-serve (async); pymupdf on failure.
    All heavy synchronous work runs off the event loop so the worker heartbeat
    keeps ticking on large documents."""
    if settings.pdf_converter == "pymupdf":
        return await asyncio.to_thread(_convert_pymupdf, pdf_bytes)
    try:
        doc = await docling_client.convert_async(
            pdf_bytes, image_export_mode="embedded",
            page_break_placeholder=_PAGE_BREAK, on_poll=on_poll,
        )
        if not (doc.get("md_content") or "").strip():
            raise DoclingServeError("empty markdown")
        return await asyncio.to_thread(_build_converted_doc, doc, pdf_bytes)
    except DoclingServeError as exc:
        logger.warning("docling-serve failed (%s); falling back to pymupdf", exc)
        return await asyncio.to_thread(_convert_pymupdf, pdf_bytes)
```

(e) Update `_convert_pymupdf`'s `return ConvertedDoc(...)` to include `page_line_starts=[]` (explicit; default also covers it).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_pdf_convert.py -v`
Expected: PASS (existing tests must be updated if they call `convert_pdf` — they monkeypatch `docling_client.convert`; switch those to `convert_async` returning a doc dict). Update the two existing async tests accordingly.

- [ ] **Step 5: Commit**

```bash
git add app/services/pdf_convert.py tests/test_pdf_convert.py
git commit -m "feat(pdf): convert_pdf async + off-loop transform + page offsets"
```

---

### Task 4: Robust heading matching + no-drop page fallback in `split_into_segments`

**Files:**
- Modify: `backend/app/services/pdf_convert.py`
- Test: `backend/tests/test_pdf_split.py`

**Interfaces:**
- Consumes: `ConvertedDoc.page_line_starts` (Task 3).
- Produces: `_find_heading_line` matches on a normalized core (HTML-unescaped, casefolded, non-alphanumerics stripped); `split_into_segments` never drops an outline entry — an unmatched title falls back to its `page_start` line via `page_line_starts`.

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/test_pdf_split.py`:

```python
def test_numbered_title_matches_spaced_heading():
    from app.services.pdf_convert import _heading_lines, _find_heading_line
    lines = "# 1 Preface\n\nbody\n".split("\n")
    hl = _heading_lines(lines)
    # outline title has the number glued + a curly apostrophe variant elsewhere
    assert _find_heading_line(hl, "1Preface", 0) == 0


def test_unmatched_outline_entry_not_dropped_uses_page_fallback():
    # 'Hidden' has no heading in the markdown; page_line_starts maps page 1 → line 3
    md = "# Intro\n\nintro body\n# Real\n\nreal body\n"
    conv = ConvertedDoc(markdown=md, headings=[], page_texts=[md, "p2"],
                        table_pages=set(), images=[], engine="docling",
                        page_line_starts=[0, 3])
    outline = [
        Segment(title="Intro", level=1, page_start=0, page_end=0, path=["Intro"]),
        Segment(title="Hidden", level=1, page_start=1, page_end=1, path=["Hidden"]),
    ]
    segs = split_into_segments(conv, outline)
    titles = [s.title for s in segs]
    assert "Hidden" in titles            # NOT dropped
    assert len(segs) == 2
```

(Ensure `ConvertedDoc` is imported in this test module.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_pdf_split.py -k "numbered_title or unmatched_outline" -v`
Expected: FAIL (current matching misses; unmatched entry is dropped).

- [ ] **Step 3: Implement**

In `backend/app/services/pdf_convert.py`:

(a) Add imports: `import html` (top of file).

(b) Add a normalization helper and rewrite `_find_heading_line`:

```python
def _norm_core(s: str) -> str:
    """Match key: unescape entities, casefold, strip all non-alphanumerics.
    Makes '1Preface' == '1 Preface' and "What's" == 'What’s'."""
    return re.sub(r"[^a-z0-9]+", "", html.unescape(s).lower())


def _find_heading_line(headings: list[tuple[int, str]], title: str, start: int) -> "int | None":
    t = _norm_core(title)
    if not t:
        return None
    # exact-core match first (safest), then containment as a secondary.
    for idx, text in headings:
        if idx >= start and _norm_core(text) == t:
            return idx
    for idx, text in headings:
        if idx < start:
            continue
        h = _norm_core(text)
        if t in h or h in t:
            return idx
    return None
```

(c) In `split_into_segments`, replace the outline loop's drop (`continue`) with a page-provenance fallback. Replace this block:

```python
    if outline:
        cursor = 0
        for seg in outline:
            line = _find_heading_line(heading_lines, seg.title, cursor)
            if line is None:
                continue
            cursor = line + 1
            boundaries.append((line, seg.title, seg.level, seg.path or [seg.title],
                               seg.page_start, seg.page_end))
```

with:

```python
    if outline:
        cursor = 0
        starts = converted.page_line_starts
        for seg in outline:
            line = _find_heading_line(heading_lines, seg.title, cursor)
            if line is None:
                # Never drop: fall back to the page where the entry begins.
                if starts and 0 <= seg.page_start < len(starts):
                    line = max(starts[seg.page_start], cursor)
                else:
                    line = cursor
                logger.info("split: %r not found as heading; page-fallback line %d",
                            seg.title, line)
            cursor = line + 1
            boundaries.append((line, seg.title, seg.level, seg.path or [seg.title],
                               seg.page_start, seg.page_end))
```

(Ensure `logger` exists in the module — it does.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_pdf_split.py -v`
Expected: PASS (all, including existing no-bleed/table tests).

- [ ] **Step 5: Commit**

```bash
git add app/services/pdf_convert.py tests/test_pdf_split.py
git commit -m "fix(pdf): robust heading match + page fallback (no dropped outline entries)"
```

---

### Task 5: Extract `escalate_segments` (exclusive-page guard + logging)

**Files:**
- Modify: `backend/app/services/pdf_escalate.py`
- Test: `backend/tests/test_pdf_escalate_batch.py` (create)

**Interfaces:**
- Consumes: `RenderedSegment`, `ConvertedDoc`, `score_segment`, `escalate_segment`, `settings.pdf_vlm_*`.
- Produces: `async def escalate_segments(pdf_bytes, segments, converted, on_event=None) -> None` — mutates `segments` in place: flags low-confidence segments, escalates only those that **exclusively own their page range**, within `pdf_vlm_max_pages_per_run`; logs per escalation; awaits `on_event(done, total, title)` if given. No-op when `pdf_vlm_escalation_enabled` is False.

This moves the escalation logic currently embedded in `pdf_import.build_segments` into a focused, testable unit.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_pdf_escalate_batch.py
import os, sys
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import app.services.pdf_escalate as esc
from app.services.pdf_convert import ConvertedDoc, RenderedSegment


def _seg(title, p0, p1, md):
    return RenderedSegment(title=title, level=1, path=[title], page_start=p0,
                           page_end=p1, markdown=md, images=[])


@pytest.mark.asyncio
async def test_escalate_segments_only_exclusive_flagged(monkeypatch):
    # 'Bad' owns page 0 and is flagged (ragged table) → escalated.
    # 'Shared'/'Other' both on page 1 → flagged but shared → skipped.
    bad = _seg("Bad", 0, 0, "## Bad\n\n| a | b |\n| --- | --- |\n| 1 | 2 | 3 |\n")
    shared = _seg("Shared", 1, 1, "## Shared\n\n| a | b |\n| --- | --- |\n| 1 | 2 | 3 |\n")
    other = _seg("Other", 1, 1, "## Other\n\nplain\n")
    segs = [bad, shared, other]
    conv = ConvertedDoc(markdown="", headings=[], page_texts=["x"*50, "y"*50],
                        table_pages=set(), images=[], engine="docling")
    monkeypatch.setattr(esc.settings, "pdf_vlm_escalation_enabled", True)
    monkeypatch.setattr(esc.settings, "pdf_vlm_max_pages_per_run", 30)
    calls = []
    async def fake_one(pdf_bytes, segment):
        calls.append(segment.title)
        return "## Bad\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n"
    monkeypatch.setattr(esc, "escalate_segment", fake_one)

    await esc.escalate_segments(b"%PDF", segs, conv)
    assert calls == ["Bad"]
    assert "| 1 | 2 | 3 |" not in bad.markdown


@pytest.mark.asyncio
async def test_escalate_segments_disabled_noop(monkeypatch):
    seg = _seg("Bad", 0, 0, "## Bad\n\n| a | b |\n| --- | --- |\n| 1 | 2 | 3 |\n")
    conv = ConvertedDoc(markdown="", headings=[], page_texts=["x"*50],
                        table_pages=set(), images=[], engine="docling")
    monkeypatch.setattr(esc.settings, "pdf_vlm_escalation_enabled", False)
    called = []
    async def fake_one(b, s): called.append(s.title); return "x"
    monkeypatch.setattr(esc, "escalate_segment", fake_one)
    await esc.escalate_segments(b"%PDF", [seg], conv)
    assert called == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_pdf_escalate_batch.py -v`
Expected: FAIL — `escalate_segments` not defined.

- [ ] **Step 3: Implement**

Append to `backend/app/services/pdf_escalate.py` (add `from app.core.config import settings` if not already imported):

```python
async def escalate_segments(pdf_bytes, segments, converted, on_event=None) -> None:
    """Re-convert low-confidence segments in place via the VLM, but only those
    that exclusively own their page range (escalating a shared page would pull in
    neighbours' content and reintroduce cross-section bleed). Bounded by the
    per-run page budget."""
    if not settings.pdf_vlm_escalation_enabled:
        return

    page_owners: dict[int, int] = {}
    for s in segments:
        for p in range(s.page_start, s.page_end + 1):
            page_owners[p] = page_owners.get(p, 0) + 1

    def _exclusive(s) -> bool:
        return all(page_owners.get(p, 0) == 1 for p in range(s.page_start, s.page_end + 1))

    flagged = [s for s in segments if _exclusive(s) and score_segment(s, converted)]
    if flagged:
        logger.info("pdf_escalate: %d/%d segments flagged; re-converting via VLM",
                    len(flagged), len(segments))
    budget = settings.pdf_vlm_max_pages_per_run
    total = len(flagged)
    done = 0
    for seg in flagged:
        pages = seg.page_end - seg.page_start + 1
        if pages > budget:
            continue
        new_md = await escalate_segment(pdf_bytes, seg)
        if new_md != seg.markdown:
            seg.markdown = new_md
            matched = [img for img in converted.images if img.filename in new_md]
            seg.images = matched or seg.images
        budget -= pages
        done += 1
        logger.info("pdf_escalate: %d/%d re-converted (%r)", done, total, seg.title)
        if on_event is not None:
            try:
                await on_event(done, total, seg.title)
            except Exception:  # noqa: BLE001
                logger.exception("escalate on_event failed")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_pdf_escalate_batch.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/pdf_escalate.py tests/test_pdf_escalate_batch.py
git commit -m "refactor(pdf): extract escalate_segments (exclusive-page guard + logging)"
```

---

### Task 6: Rewire `run_pdf_extraction` — phases, denominator, logs, on_poll

**Files:**
- Modify: `backend/app/services/pdf_import.py`
- Test: `backend/tests/test_pdf_pipeline_integration.py`

**Interfaces:**
- Consumes: `convert_pdf(on_poll=...)`, `split_into_segments`, `escalate_segments` (Tasks 3-5), `_outline_segments`.
- Produces: `build_segments` is removed; `run_pdf_extraction` orchestrates `convert→split→escalate` directly, computing the outline once (denominator set before convert), setting `current_phase` per step, emitting logs, and passing an `on_poll` that logs liveness. `_outline_for(pdf_bytes)` stays.

- [ ] **Step 1: Update the integration test**

Rewrite `backend/tests/test_pdf_pipeline_integration.py` to target the new structure. Replace its two `build_segments` tests with calls through `run_pdf_extraction`'s helpers. Since `run_pdf_extraction` needs a DB, test the orchestration helper instead — add a module-level coroutine `process_segments(pdf_bytes, outline, on_poll=None) -> tuple[list, ConvertedDoc]` that does convert+split+escalate (no DB), and test it:

```python
import os, sys
import fitz, pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import app.services.pdf_import as pi
from app.services.pdf_convert import ConvertedDoc


def _outline_pdf():
    d = fitz.open()
    for _ in range(2): d.new_page()
    d.set_toc([[1, "Alpha Section", 1], [1, "Beta Section", 1]])
    return d.tobytes()


@pytest.mark.asyncio
async def test_process_segments_splits_and_calls_on_poll(monkeypatch):
    md = "## Alpha Section\n\nAlpha body.\n\n## Beta Section\n\nBeta body.\n"
    async def fake_convert(pdf_bytes, on_poll=None):
        if on_poll: await on_poll({"task_status": "started", "task_position": 0})
        return ConvertedDoc(markdown=md, headings=[], page_texts=[md, ""],
                            table_pages=set(), images=[], engine="docling")
    monkeypatch.setattr(pi, "convert_pdf", fake_convert)
    monkeypatch.setattr(pi.settings, "pdf_vlm_escalation_enabled", False)
    polls = []
    async def on_poll(s): polls.append(s["task_status"])
    outline = pi._outline_for(_outline_pdf())
    segs, conv = await pi.process_segments(_outline_pdf(), outline, on_poll=on_poll)
    assert [s.title for s in segs] == ["Alpha Section", "Beta Section"]
    assert "Beta" not in segs[0].markdown
    assert polls == ["started"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_pdf_pipeline_integration.py -v`
Expected: FAIL — `process_segments` not defined.

- [ ] **Step 3: Implement**

In `backend/app/services/pdf_import.py`:

(a) Update imports: `from app.services.pdf_convert import (ConvertedDoc, RenderedImage, RenderedSegment, convert_pdf, split_into_segments)` and `from app.services.pdf_escalate import escalate_segments` (drop `score_segment`/`escalate_segment` direct imports).

(b) Replace `build_segments` with `process_segments`:

```python
async def process_segments(pdf_bytes, outline, on_poll=None):
    """Convert (off-loop, async), split on headings, then VLM-escalate the
    exclusive low-confidence segments. Returns (segments, converted)."""
    converted = await convert_pdf(pdf_bytes, on_poll=on_poll)
    segments = split_into_segments(converted, outline)
    await escalate_segments(pdf_bytes, segments, converted)
    return segments, converted
```

(c) In `run_pdf_extraction`, replace the segmentation/convert block (currently sets `pdf_convert`, defines `_convert_progress`, calls `build_segments`, sets `articles_total`) with:

```python
    outline = await asyncio.to_thread(_outline_for, pdf_bytes)
    run.articles_total = len(outline)
    run.current_phase = "pdf_convert"
    await db.commit()
    logger.info("pdf_convert: converting %d-outline-entry PDF via docling-serve", len(outline))

    _t0 = time.monotonic()
    async def _on_poll(status: dict) -> None:
        logger.info("pdf_convert: still processing (status=%s, queue=%s, %.0fs elapsed)",
                    status.get("task_status"), status.get("task_position"),
                    time.monotonic() - _t0)

    converted = await convert_pdf(pdf_bytes, on_poll=_on_poll)
    run.current_phase = "pdf_split"
    await db.commit()
    rendered_segments = split_into_segments(converted, outline)
    logger.info("pdf_split: %d article segments (%s engine)",
                len(rendered_segments), converted.engine)

    run.current_phase = "pdf_escalate"
    await db.commit()
    await escalate_segments(pdf_bytes, rendered_segments, converted)
    run.articles_total = len(rendered_segments)
    await db.commit()
```

Add `import time` at the top if absent. Keep everything after (the `delete(TOCEntry)`, the article-build loop, `content_scraping` phase, persist loop, `_reconcile_removals`, completion) unchanged — it consumes `rendered_segments`.

(d) Remove the now-unused `build_segments` and its old `_convert_progress`.

- [ ] **Step 4: Run tests**

Run: `pytest tests/ -k pdf -v`
Expected: PASS. Fix any test still referencing `build_segments` (retarget to `process_segments`).

- [ ] **Step 5: Commit**

```bash
git add app/services/pdf_import.py tests/test_pdf_pipeline_integration.py
git commit -m "feat(pdf): phase reporting, early denominator, liveness logs in run_pdf_extraction"
```

---

### Task 7: Frontend phase labels for PDF phases

**Files:**
- Modify: `frontend/src/components/SourceList.tsx` (the `renderRunResult` running branch, ~line 414-448)

**Interfaces:** none (UI only).

- [ ] **Step 1: Add the phase mapping**

In `renderRunResult`, inside the `if (run.status === "running")` block, right after the existing `toc_discovery` check, add:

```tsx
      const indeterminatePdf: Record<string, string> = {
        pdf_acquire: "Downloading PDF…",
        pdf_convert: "Converting document…",
        pdf_split: "Splitting into articles…",
        pdf_escalate: "Refining low-confidence sections…",
      };
      if (run.current_phase && indeterminatePdf[run.current_phase]) {
        return (
          <div className="run-progress">
            <span className="run-phase">{indeterminatePdf[run.current_phase]}</span>
            <div className="progress-bar indeterminate" />
          </div>
        );
      }
```

- [ ] **Step 2: Type-check + build**

Run: `cd frontend && npm run build`
Expected: builds with no type errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/SourceList.tsx
git commit -m "feat(frontend): friendly indeterminate progress for PDF phases"
```

---

### Task 8: Live validation

**Files:** none (manual validation against the running cluster; append a "Validation result" note to the spec).

**Interfaces:** none.

- [ ] **Step 1: Validate CloudAlly (segment-drop) locally against live docling-serve**

From `backend/`, with the docling key in env and the CloudAlly PDF at `$SCRATCH/cloudally.pdf`:

```bash
SCRATCH=<scratchpad> DOCEXTRACTOR_DOCLING_SERVE_API_KEY=<key> \
DOCEXTRACTOR_PDF_VLM_ESCALATION_ENABLED=false python3 -c "
import asyncio, app.services.pdf_import as pi
data=open('$SCRATCH/cloudally.pdf','rb').read()
outline=pi._outline_for(data)
segs,conv=asyncio.run(pi.process_segments(data, outline))
print('outline', len(outline), 'segments', len(segs), 'engine', conv.engine)
"
```
Expected: `segments` ≈ `outline` (119, not 14).

- [ ] **Step 2: Deploy + re-extract on the cluster, watch monitoring**

After the PR merges and CI builds the image, deploy (per the k8s runbook, pinning the new `sha-`), then re-extract CloudAlly and the HYCU User Guides (clear their completed-run `pdf_hash` first so the byte-hash fast-path doesn't skip). Confirm via DB:
- CloudAlly: `articles_total` ≈ 119, removed count ~0 afterward.
- User Guides: run COMPLETES without reaping (no `attempts` climb), `log_text` shows `pdf_convert`/poll/`pdf_split` lines.

- [ ] **Step 3: Record result**

Append a "Validation result" section to `docs/superpowers/specs/2026-06-28-pdf-pipeline-reliability-design.md`.

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/specs/2026-06-28-pdf-pipeline-reliability-design.md
git commit -m "docs(pdf): record pipeline-reliability validation result"
```

---

## Self-Review

**Spec coverage:**
- Problem 1 (event-loop starvation): off-loop `to_thread` for json parse (Task 2), transform + pymupdf fallback (Task 3). ✓
- Problem 2 (monitoring): async polling + on_poll (Task 2), page-offset map (Task 3), phases/denominator/logs (Task 6), frontend labels (Task 7), poll-interval setting (Task 1), escalation no longer abuses articles_extracted (Task 5). ✓
- Problem 3 (segment drop): normalization + page fallback (Task 4). ✓
- Never-no-output fallback preserved (Task 3). ✓
- Validation incl. CloudAlly 119 + User Guides no-reap (Task 8). ✓

**Placeholder scan:** No TBD/TODO; every code step has full code. The only "verify live" item (Task 2 Step 5: terminal status strings) is a concrete probe with a correction path, not deferred work.

**Type consistency:** `convert_async(...) -> dict`, `convert_pdf(pdf_bytes, on_poll=None) -> ConvertedDoc`, `_build_converted_doc(doc, pdf_bytes)`, `ConvertedDoc(..., page_line_starts)`, `split_into_segments(converted, outline)`, `escalate_segments(pdf_bytes, segments, converted, on_event=None)`, `process_segments(pdf_bytes, outline, on_poll=None) -> (segments, converted)`, `_norm_core`, `_PAGE_BREAK` — names/signatures consistent across tasks. `build_segments` is removed in Task 6 and no later task references it.
