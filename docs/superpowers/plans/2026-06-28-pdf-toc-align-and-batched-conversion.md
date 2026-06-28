# PDF TOC Alignment + Page-Batched Conversion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop PDF same-page sections from collapsing onto one TOC entry, and convert oversized PDFs through docling in page batches so they don't OOM the server.

**Architecture:** Restrict `_reconcile_removals`' URL re-link to articles whose `toc_entry_id` is NULL (so it no longer clobbers the correct per-segment links). Add page-batched docling conversion: split big PDFs into `page_range` chunks, convert each via the existing `convert_async`, and merge the result dicts before the existing `_build_converted_doc` stitches them (docling reports absolute page numbers, so no offset).

**Tech Stack:** Python 3, FastAPI, SQLAlchemy async, httpx, PyMuPDF (`fitz`), pytest.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-06-28-pdf-toc-align-and-batched-conversion-design.md`.
- docling-serve reports **absolute** page numbers for `page_range` requests (verified: `[7,9]`→`page_no` 7,8,9) — batch stitching needs no offset.
- The reconcile re-link must become NULL-only; it must remain a no-op for web (unique URLs) and keep the existing three `test_reconcile_removals.py` tests green.
- Batching reuses `convert_async` (already accepts `page_range`) and `_build_converted_doc` (strips `_PAGE_BREAK` placeholders, content-addresses images, parses headings/tables). Heavy sync work stays under `asyncio.to_thread`.
- Any batch failure → propagate `DoclingServeError` so `convert_pdf` falls back to whole-doc `_convert_pymupdf` (never "no output").
- Tests: `python3`/`pytest` from `backend/` (no `python`). Async DB tests use the `docextractor_test` async harness already in `tests/test_reconcile_removals.py` / `tests/test_pdf_run_extraction.py`; convert tests mock the docling client with `fitz`-built PDFs.
- Secrets env-only; `backend/.env` is git-tracked — never write keys there.

---

### Task 1: Add `pdf_convert_batch_pages` setting

**Files:**
- Modify: `backend/app/core/config.py` (after `docling_serve_poll_interval`)
- Test: `backend/tests/test_pdf_convert_settings.py`

**Interfaces:**
- Produces: `settings.pdf_convert_batch_pages: int` (default `80`).

- [ ] **Step 1: Extend the settings test**

In `backend/tests/test_pdf_convert_settings.py`, add to `test_pdf_converter_defaults`:

```python
    assert s.pdf_convert_batch_pages == 80
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_pdf_convert_settings.py::test_pdf_converter_defaults -v`
Expected: FAIL — no attribute `pdf_convert_batch_pages`.

- [ ] **Step 3: Add the setting**

In `backend/app/core/config.py`, immediately after the `docling_serve_poll_interval` line:

```python
    # Large PDFs are converted through docling in page-range batches of this many
    # pages, so docling-serve doesn't load a whole 150+ page doc at once (OOM).
    # A doc with <= this many pages is converted in a single call.
    pdf_convert_batch_pages: int = 80
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_pdf_convert_settings.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/core/config.py tests/test_pdf_convert_settings.py
git commit -m "feat(pdf): add pdf_convert_batch_pages setting"
```

---

### Task 2: Restrict `_reconcile_removals` re-link to NULL toc links

**Files:**
- Modify: `backend/app/services/firecrawl.py` (the re-link `update` in `_reconcile_removals`, ~line 1279-1283)
- Test: `backend/tests/test_reconcile_removals.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `_reconcile_removals` only fills `toc_entry_id` where it is NULL; already-linked articles (set by `process_article_result` this run) are untouched.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_reconcile_removals.py`:

```python
@pytest.mark.asyncio
async def test_same_url_articles_keep_distinct_toc_links(db):
    """PDF sections sharing a #page URL, each already linked to its OWN TOC entry,
    must NOT be collapsed onto one entry by the URL re-link."""
    source_id = await _source(db)
    run_id = await _run(db, source_id)
    te1 = _toc(source_id, "http://x/doc#page=6", 0)
    te2 = _toc(source_id, "http://x/doc#page=6", 1)  # same url, different entry
    db.add_all([te1, te2])
    await db.flush()
    a1 = _article(source_id, "http://x/doc#page=6", toc_entry_id=te1.id)
    a1.topic_key = "k1"
    a2 = _article(source_id, "http://x/doc#page=6", toc_entry_id=te2.id)
    a2.topic_key = "k2"
    db.add_all([a1, a2])
    await db.commit()

    await firecrawl_service._reconcile_removals(db, source_id, run_id)

    rows = {a.topic_key: a for a in (await db.execute(select(Article))).scalars()}
    assert rows["k1"].toc_entry_id == te1.id
    assert rows["k2"].toc_entry_id == te2.id
    assert rows["k1"].removed_at is None and rows["k2"].removed_at is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_reconcile_removals.py::test_same_url_articles_keep_distinct_toc_links -v`
Expected: FAIL — both articles get collapsed to `te1.id` (so `k2` link assertion fails).

- [ ] **Step 3: Restrict the re-link to NULL links**

In `backend/app/services/firecrawl.py`, in `_reconcile_removals`, change the re-link `update` to only touch NULL `toc_entry_id`:

```python
        await db.execute(
            update(Article)
            .where(
                Article.source_id == source_id,
                Article.toc_entry_id.is_(None),
            )
            .values(toc_entry_id=relink)
        )
```

(Also update the method's docstring sentence that says it re-links "every article" to say it re-links only articles still NULL after `process_article_result` — i.e. pages a resumed run skipped — so the comment matches the code.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_reconcile_removals.py -v`
Expected: PASS — the new test plus the three existing tests (all of which start with `toc_entry_id=None`, so the NULL-only re-link still handles them).

- [ ] **Step 5: Commit**

```bash
git add app/services/firecrawl.py tests/test_reconcile_removals.py
git commit -m "fix(pdf): reconcile re-link only NULL toc links (no same-page collapse)"
```

---

### Task 3: Page-batched docling conversion

**Files:**
- Modify: `backend/app/services/pdf_convert.py` (`convert_pdf`; add `_page_count`, `_page_batches`, `_merge_docling_docs`, `_convert_docling_batched`)
- Test: `backend/tests/test_pdf_convert.py`

**Interfaces:**
- Consumes: `docling_client.convert_async(..., page_range=...)`, `_PAGE_BREAK`, `_build_converted_doc` (existing).
- Produces:
  - `def _page_count(pdf_bytes: bytes) -> int`
  - `def _page_batches(page_count: int, size: int) -> list[tuple[int, int]]` — 1-based inclusive ranges.
  - `def _merge_docling_docs(batch_docs: list[dict]) -> dict` — `md_content` joined by `\n{_PAGE_BREAK}\n`; `json_content.texts`/`.tables` concatenated.
  - `async def _convert_docling_batched(pdf_bytes, page_count, on_poll=None) -> dict`
  - `convert_pdf` branches single vs batched on `page_count > settings.pdf_convert_batch_pages`.

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/test_pdf_convert.py`:

```python
def test_page_batches_ranges():
    assert pc._page_batches(5, 2) == [(1, 2), (3, 4), (5, 5)]
    assert pc._page_batches(80, 80) == [(1, 80)]
    assert pc._page_batches(0, 80) == []


def test_merge_docling_docs_joins_with_placeholder():
    from app.services import docling_client as dc
    a = {"md_content": "A", "json_content": {"texts": [{"t": 1}], "tables": [{"x": 1}]}}
    b = {"md_content": "B", "json_content": {"texts": [{"t": 2}], "tables": []}}
    m = pc._merge_docling_docs([a, b])
    assert m["md_content"] == f"A\n{dc._PAGE_BREAK}\nB"
    assert m["json_content"]["texts"] == [{"t": 1}, {"t": 2}]
    assert m["json_content"]["tables"] == [{"x": 1}]


def _npage_pdf(n):
    import fitz
    d = fitz.open()
    for i in range(n):
        d.new_page().insert_text((72, 72), f"Page {i+1} body")
    return d.tobytes()


@pytest.mark.asyncio
async def test_convert_pdf_batches_large_doc(monkeypatch):
    from app.services import docling_client as dc
    monkeypatch.setattr(pc.settings, "pdf_converter", "docling")
    monkeypatch.setattr(pc.settings, "pdf_convert_batch_pages", 2)
    calls = []

    async def fake_convert_async(pdf_bytes, **kw):
        rng = kw["page_range"]
        calls.append(rng)
        pages = [f"# Page {p}" for p in range(rng[0], rng[1] + 1)]
        return {"md_content": f"\n{dc._PAGE_BREAK}\n".join(pages),
                "json_content": {"texts": [], "tables": []}}

    monkeypatch.setattr(pc.docling_client, "convert_async", fake_convert_async)
    out = await pc.convert_pdf(_npage_pdf(5))
    assert calls == [(1, 2), (3, 4), (5, 5)]          # batched
    assert out.engine == "docling"
    assert len(out.page_line_starts) == 5             # all 5 pages stitched
    assert dc._PAGE_BREAK not in out.markdown


@pytest.mark.asyncio
async def test_convert_pdf_single_call_for_small_doc(monkeypatch):
    monkeypatch.setattr(pc.settings, "pdf_converter", "docling")
    monkeypatch.setattr(pc.settings, "pdf_convert_batch_pages", 80)
    calls = []

    async def fake_convert_async(pdf_bytes, **kw):
        calls.append(kw.get("page_range"))
        return {"md_content": "# Only", "json_content": {"texts": [], "tables": []}}

    monkeypatch.setattr(pc.docling_client, "convert_async", fake_convert_async)
    out = await pc.convert_pdf(_npage_pdf(3))
    assert calls == [None]                             # single call, no page_range
    assert out.engine == "docling"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_pdf_convert.py -k "batches or merge or page_batches or single_call" -v`
Expected: FAIL — `_page_batches`/`_merge_docling_docs` undefined; `convert_pdf` doesn't batch.

- [ ] **Step 3: Implement**

In `backend/app/services/pdf_convert.py`, add helpers (near `_page_texts`):

```python
def _page_count(pdf_bytes: bytes) -> int:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        return doc.page_count
    finally:
        doc.close()


def _page_batches(page_count: int, size: int) -> list[tuple[int, int]]:
    """1-based inclusive page ranges of at most `size` pages."""
    return [(s + 1, min(s + size, page_count)) for s in range(0, page_count, size)]


def _merge_docling_docs(batch_docs: list[dict]) -> dict:
    """Stitch per-batch docling result dicts into one. docling reports absolute
    page numbers, so texts/tables concatenate without offset; markdowns join with
    the page-break placeholder so the merged page stream stays continuous."""
    mds = [(d.get("md_content") or "") for d in batch_docs]
    texts: list = []
    tables: list = []
    for d in batch_docs:
        jc = d.get("json_content") or {}
        texts.extend(jc.get("texts") or [])
        tables.extend(jc.get("tables") or [])
    return {
        "md_content": ("\n" + _PAGE_BREAK + "\n").join(mds),
        "json_content": {"texts": texts, "tables": tables},
    }


async def _convert_docling_batched(pdf_bytes: bytes, page_count: int, on_poll=None) -> dict:
    batch_docs: list[dict] = []
    for start, end in _page_batches(page_count, settings.pdf_convert_batch_pages):
        doc = await docling_client.convert_async(
            pdf_bytes, page_range=(start, end), image_export_mode="embedded",
            page_break_placeholder=_PAGE_BREAK, on_poll=on_poll,
        )
        batch_docs.append(doc)
    return _merge_docling_docs(batch_docs)
```

Then rewrite `convert_pdf` to branch on page count:

```python
async def convert_pdf(pdf_bytes: bytes, on_poll=None) -> ConvertedDoc:
    """Convert a whole PDF to markdown via docling-serve (async); pymupdf on failure.
    Large PDFs are converted in page-range batches so docling-serve doesn't OOM.
    All heavy synchronous work runs off the event loop so the worker heartbeat
    keeps ticking on large documents."""
    if settings.pdf_converter == "pymupdf":
        return await asyncio.to_thread(_convert_pymupdf, pdf_bytes)
    try:
        page_count = await asyncio.to_thread(_page_count, pdf_bytes)
        if page_count > settings.pdf_convert_batch_pages:
            doc = await _convert_docling_batched(pdf_bytes, page_count, on_poll)
        else:
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

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_pdf_convert.py -v`
Expected: PASS (new batching tests + existing convert tests; the existing single-call tests still pass because their PDFs are ≤ the patched/default threshold).

- [ ] **Step 5: Commit**

```bash
git add app/services/pdf_convert.py tests/test_pdf_convert.py
git commit -m "feat(pdf): page-batched docling conversion for large PDFs"
```

---

### Task 4: Live validation + heal CloudAlly

**Files:** none (manual; append a "Validation result" note to the spec).

**Interfaces:** none.

- [ ] **Step 1: Local end-to-end against live docling-serve**

From `backend/`, with the docling key in env and `$SCRATCH/cloudally.pdf` present, confirm the stitched/aligned result and batching:

```bash
SCRATCH=<scratchpad> DOCEXTRACTOR_DOCLING_SERVE_API_KEY=<key> \
DOCEXTRACTOR_PDF_VLM_ESCALATION_ENABLED=false python3 -c "
import asyncio, app.services.pdf_import as pi
from app.services import pdf_convert as pc
data=open('$SCRATCH/cloudally.pdf','rb').read()
outline=pi._outline_for(data)
segs,conv=asyncio.run(pi.process_segments(data, outline))
print('engine', conv.engine, 'outline', len(outline), 'segments', len(segs),
      'pages', pc._page_count(data), 'batched', pc._page_count(data) > 80)
"
```
Expected: `engine docling`, segments ≈ outline (≈119); for the 152-page User Guides, `batched True`.

- [ ] **Step 2: Deploy + heal data**

After merge + CI, deploy (k8s runbook, pin the new `sha-`). Then re-extract CloudAlly and the HYCU User Guides (clear their completed-run `pdf_hash` first so the byte-hash fast-path doesn't skip). Confirm via DB:
- CloudAlly: `select count(distinct toc_entry_id) from articles where source_id=<cloudally> and removed_at is null` equals the live-article count (1 article per TOC entry — no collapse).
- User Guides: run COMPLETES at `attempts=1` and the stored articles' engine is docling (no OOM/502 churn).

- [ ] **Step 3: Record result**

Append a "Validation result" section to `docs/superpowers/specs/2026-06-28-pdf-toc-align-and-batched-conversion-design.md`.

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/specs/2026-06-28-pdf-toc-align-and-batched-conversion-design.md
git commit -m "docs(pdf): record toc-align + batched-conversion validation"
```

---

## Self-Review

**Spec coverage:**
- Problem 1 (TOC misalignment): NULL-only re-link → Task 2 (with same-page test). ✓
- Problem 2 (OOM): `pdf_convert_batch_pages` setting (Task 1) + batched conversion/merge/stitch (Task 3). ✓
- Absolute-page stitching (no offset): `_merge_docling_docs` concatenates texts/tables directly (Task 3). ✓
- Reuse `_build_converted_doc` + `to_thread` offload preserved (Task 3). ✓
- Batch failure → pymupdf fallback (Task 3 `convert_pdf` except). ✓
- Existing reconcile tests stay green (NULL-start) (Task 2 Step 4). ✓
- Validation incl. CloudAlly alignment + User Guides batched-docling (Task 4). ✓

**Placeholder scan:** No TBD/TODO; every code step has full code; Task 4's "<key>" is a runtime secret placeholder for a manual command, not deferred work.

**Type consistency:** `_page_count(pdf_bytes)->int`, `_page_batches(page_count,size)->list[tuple[int,int]]`, `_merge_docling_docs(batch_docs)->dict`, `_convert_docling_batched(pdf_bytes,page_count,on_poll=None)->dict`, `convert_pdf(pdf_bytes,on_poll=None)->ConvertedDoc` — consistent across Task 3 definition and tests. `_PAGE_BREAK`/`_build_converted_doc`/`convert_async(page_range=...)` reused with their existing signatures.
