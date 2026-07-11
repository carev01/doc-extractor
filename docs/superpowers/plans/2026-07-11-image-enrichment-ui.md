# Image-Enrichment Monitoring + On-Demand Enrich — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let operators see per-source and corpus-wide image-enrichment progress in the UI, and run enrichment on demand (drain all missing images for a source) without a full re-scrape.

**Architecture:** A new lightweight `kind="enrich"` extraction run (mirrors the PDF `kind="escalate"` retry) runs only the image-enrichment phase, draining all of a source's missing images. A stats endpoint exposes per-source described/pending counts; the frontend shows a per-source badge + action and a Dashboard rollup. Two downstream-safety folds: the delta record serves `sha256(content_markdown)` as `content_hash`, and enrich runs fire `extraction_complete`.

**Tech Stack:** FastAPI, SQLAlchemy (async/asyncpg; sync/psycopg2 in tests), Pydantic v2, PostgreSQL; React 19 + TypeScript + Vite + Axios.

## Global Constraints

- No DB migration — reuses existing `article_images` columns and the `extraction_runs.kind` string.
- **Enrichment never modifies the Article's `content_hash`** (raw-scrape fingerprint). The delta-record `content_hash` change (Task 4) is a *serve-time* computation only; it does not write the Article.
- The manual enrich run **drains all** missing images for the source (ignores `image_vlm_max_per_run`); extraction-time enrichment keeps the budget.
- Enrichment is best-effort (rolls back its own failures); a run must never be left stuck `EXTRACTING`.
- `pending` (UI/stats) = images where `description IS NULL AND is_meaningful IS NOT FALSE` (unevaluated or meaningful-undescribed; decorative `is_meaningful = false` excluded).
- Backend tasks are TDD (data-layer tests sync `psycopg2`; async routes via `httpx.AsyncClient`; run tests with `backend/venv/bin/pytest`). The **frontend has no test suite** — verify frontend tasks with `npm run build` (tsc) + `npm run lint`, following existing component patterns.
- Commit trailer on every commit:
  ```
  Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01LZoiNMkURTEexS4UEY8rF4
  ```
- Reference spec: `docs/superpowers/specs/2026-07-11-image-enrichment-ui-design.md`.

---

## File Structure

- `app/services/image_describe.py` — **modify** — `enrich_run_images` gains `max_new` + returns a count.
- `app/services/firecrawl.py` — **modify** — add `enrich_source_run`.
- `app/worker.py` — **modify** — dispatch `kind="enrich"`.
- `app/routes/extraction.py` — **modify** — `POST /api/extraction/enrich/{source_id}`.
- `app/services/delta_feed.py` — **modify** — `_content_record` serves `sha256(content_markdown)`.
- `app/routes/dashboard.py` + `app/schemas/dashboard.py` — **modify** — `GET /api/dashboard/enrichment`.
- `frontend/src/types/index.ts` + `frontend/src/api/client.ts` — **modify** — types + client fns; `JobsView.tsx` enrich label.
- `frontend/src/components/SourceList.tsx` — **modify** — per-source badge + button.
- `frontend/src/components/Dashboard.tsx` — **modify** — enrichment section.
- Tests: `backend/tests/test_enrich_run_budget.py`, `test_enrich_endpoint.py`, `test_enrich_source_run.py`, `test_delta_served_hash.py`, `test_enrichment_stats.py`.

---

## Task 1: `enrich_run_images` — optional unlimited budget + return count

**Files:**
- Modify: `app/services/image_describe.py`
- Test: `tests/test_enrich_run_budget.py`

**Interfaces:**
- Produces: `async def enrich_run_images(db, source_id, run_id, *, describe=describe_image, max_new: int | None = None) -> int` — `max_new=None` → `settings.image_vlm_max_per_run` (unchanged default); returns the number of images newly described this run.

- [ ] **Step 1: Write the failing test**

Create `tests/test_enrich_run_budget.py` (async harness like `tests/test_image_enrich_phase.py`; reuse its `factory` fixture shape, `_noise_png`, `_seed_article_with_image`, and `Desc`). Add:

```python
async def test_max_new_unlimited_drains_all_and_returns_count(factory, monkeypatch):
    # Default budget is 2, but max_new=None-override (a big number) describes all 3.
    monkeypatch.setattr(settings, "image_vlm_max_per_run", 2)
    src_id, run_id = await _seed_source_with_n_images(factory, 3)
    calls = {"n": 0}
    async def fake(data, alt, **kw):
        calls["n"] += 1
        return Desc(text=f"d{calls['n']}", kind="other")
    async with factory() as db:
        described = await enrich_run_images(db, src_id, run_id, describe=fake, max_new=10**9)
    assert described == 3 and calls["n"] == 3


async def test_default_still_respects_budget(factory, monkeypatch):
    monkeypatch.setattr(settings, "image_vlm_max_per_run", 2)
    src_id, run_id = await _seed_source_with_n_images(factory, 3)
    calls = {"n": 0}
    async def fake(data, alt, **kw):
        calls["n"] += 1
        return Desc(text="d", kind="other")
    async with factory() as db:
        described = await enrich_run_images(db, src_id, run_id, describe=fake)  # max_new omitted
    assert described == 2 and calls["n"] == 2
```

Add a `_seed_source_with_n_images(factory, n)` helper to the test that seeds one source with `n` articles, each with a distinct meaningful `_noise_png` image written to `settings.media_dir/<article_id>/img.png`, and returns `(source_id, run_id)`. (Model it on `_seed_article_with_image` in `test_image_enrich_phase.py`.)

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend && ./venv/bin/pytest tests/test_enrich_run_budget.py -v`
Expected: FAIL — `enrich_run_images()` got an unexpected keyword `max_new` (and no return value).

- [ ] **Step 3: Implement**

In `app/services/image_describe.py`, change the signature and budget line, add a counter, and return it:

```python
async def enrich_run_images(db: AsyncSession, source_id: uuid.UUID, run_id: uuid.UUID,
                            *, describe=describe_image, max_new: int | None = None) -> int:
```
Replace `budget = settings.image_vlm_max_per_run` with:
```python
        budget = settings.image_vlm_max_per_run if max_new is None else max_new
        described = 0
```
Increment right after `img.description, img.kind = text, kind`:
```python
                img.description, img.kind = text, kind
                article.content_markdown = inject_caption(article.content_markdown, img.local_path, text)
                changed = True
                described += 1
```
Return the count at the end of the `try` (after the loop) and `0` on the exception path:
```python
        return described
    except Exception as exc:  # noqa: BLE001 — enrichment is best-effort
        logger.warning("enrich_run_images failed for source %s: %s", source_id, exc)
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return 0
```
Also update the `if not settings.image_vlm_enabled: return` → `return 0`.

- [ ] **Step 4: Run to verify it passes**

Run: `cd backend && ./venv/bin/pytest tests/test_enrich_run_budget.py -v`
Expected: 2 passed.

- [ ] **Step 5: Regression**

Run: `cd backend && ./venv/bin/pytest tests/test_image_enrich_phase.py tests/test_image_surfacing.py -q`
Expected: pass (the extraction-time caller ignores the new return; default behavior unchanged).

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/image_describe.py backend/tests/test_enrich_run_budget.py
git commit -m "feat(enrich): enrich_run_images max_new override + returns described count"
```

---

## Task 2: `POST /api/extraction/enrich/{source_id}`

**Files:**
- Modify: `app/routes/extraction.py`
- Test: `tests/test_enrich_endpoint.py`

**Interfaces:**
- Consumes: `enqueue_run(kind="enrich")`, `settings.image_vlm_enabled`.
- Produces: `POST /api/extraction/enrich/{source_id}` → `ExtractionTriggerResponse` (status `"pending"`).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_enrich_endpoint.py` (async `httpx.AsyncClient` harness like `tests/test_delta_feed.py`; auth disabled in tests). Seed a source with images and cover:

```python
async def test_enrich_queues_run(ctx, monkeypatch):
    monkeypatch.setattr(settings, "image_vlm_enabled", True)
    c, factory = ctx
    src_id = await _seed_source_with_undescribed_image(factory)
    resp = await c.post(f"/api/extraction/enrich/{src_id}")
    assert resp.status_code == 200 and resp.json()["status"] == "pending"
    async with factory() as s:
        run = (await s.execute(select(ExtractionRun).where(ExtractionRun.source_id == src_id))).scalar_one()
        assert run.kind == "enrich" and run.status == RunStatus.PENDING


async def test_enrich_409_when_nothing_pending(ctx, monkeypatch):
    monkeypatch.setattr(settings, "image_vlm_enabled", True)
    c, factory = ctx
    src_id = await _seed_source_all_described(factory)   # every image has a description
    resp = await c.post(f"/api/extraction/enrich/{src_id}")
    assert resp.status_code == 409


async def test_enrich_409_when_disabled(ctx, monkeypatch):
    monkeypatch.setattr(settings, "image_vlm_enabled", False)
    c, factory = ctx
    src_id = await _seed_source_with_undescribed_image(factory)
    resp = await c.post(f"/api/extraction/enrich/{src_id}")
    assert resp.status_code == 409


async def test_enrich_409_when_active_run(ctx, monkeypatch):
    monkeypatch.setattr(settings, "image_vlm_enabled", True)
    c, factory = ctx
    src_id = await _seed_source_with_undescribed_image(factory)
    async with factory() as s:  # a pre-existing active run
        s.add(ExtractionRun(source_id=src_id, status=RunStatus.RUNNING)); await s.commit()
    resp = await c.post(f"/api/extraction/enrich/{src_id}")
    assert resp.status_code == 409
```

Add the seed helpers to the test file (create Vendor→Product→Source, an Article, and an `ArticleImage`; for `_seed_source_all_described` set the image's `description="x"`, `is_meaningful=True`; for undescribed leave `description=None`, `is_meaningful=None`).

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend && ./venv/bin/pytest tests/test_enrich_endpoint.py -v`
Expected: FAIL — 404 (route not defined).

- [ ] **Step 3: Implement the endpoint**

In `app/routes/extraction.py`, add these imports (top): `from app.core.config import settings`, `from app.models.image import ArticleImage`, `from sqlalchemy import func, or_` (extend the existing `from sqlalchemy import ...`). Add the route after `trigger_extraction`:

```python
@router.post("/enrich/{source_id}", response_model=ExtractionTriggerResponse)
async def trigger_enrich(
    source_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """Queue an image-enrichment-only run (kind='enrich') that describes all of a
    source's missing images without re-scraping. 409 if descriptions are disabled,
    nothing needs describing, or a run is already active for the source."""
    await authorize_source(db, principal, source_id, write=True)
    source = (await db.execute(
        select(DocumentationSource).where(DocumentationSource.id == source_id)
    )).scalar_one_or_none()
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    if not settings.image_vlm_enabled:
        raise HTTPException(status_code=409, detail="Image descriptions are not enabled")

    pending = (await db.execute(
        select(func.count())
        .select_from(ArticleImage)
        .join(Article, Article.id == ArticleImage.article_id)
        .where(
            Article.source_id == source_id,
            ArticleImage.description.is_(None),
            ArticleImage.is_meaningful.isnot(False),   # NULL or True
        )
    )).scalar()
    if not pending:
        raise HTTPException(status_code=409, detail="No images need description for this source")

    try:
        run = await enqueue_run(db, source_id, trigger="manual", kind="enrich")
    except ActiveRunExists:
        raise HTTPException(status_code=409, detail="Extraction already queued or running for this source")

    return ExtractionTriggerResponse(
        run_id=run.id, source_id=source_id, status="pending",
        message="Image enrichment queued. Poll /api/extraction/runs/{run_id} for progress.",
    )
```

> Note: `ArticleImage.is_meaningful.isnot(False)` renders `is_meaningful IS NOT FALSE` (NULL or True) — the `pending` predicate.

- [ ] **Step 4: Run to verify it passes**

Run: `cd backend && ./venv/bin/pytest tests/test_enrich_endpoint.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/routes/extraction.py backend/tests/test_enrich_endpoint.py
git commit -m "feat(enrich): POST /api/extraction/enrich/{source_id} queues a kind=enrich run"
```

---

## Task 3: Worker dispatch + `enrich_source_run`

**Files:**
- Modify: `app/worker.py`
- Modify: `app/services/firecrawl.py`
- Test: `tests/test_enrich_source_run.py`

**Interfaces:**
- Consumes: `image_describe.enrich_run_images(..., max_new=...)`, `change_log.record_run_start` / `run_change_counts`, `webhook_dispatcher`, `encode_delta_cursor`, `ContentChange`.
- Produces: `FirecrawlService.enrich_source_run(db, source_id, run_id)`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_enrich_source_run.py` (async harness; monkeypatch `settings.image_vlm_enabled=True` and `settings.media_dir`; monkeypatch `app.services.image_describe.describe_image` OR pass through — simplest: monkeypatch the module's `describe_image` used inside `enrich_run_images`… but `enrich_source_run` calls `enrich_run_images` with the default `describe`. Instead monkeypatch `image_describe.describe_image`). Seed a source with 3 undescribed meaningful images written to disk. Then:

```python
async def test_enrich_source_run_drains_and_completes(factory, monkeypatch):
    monkeypatch.setattr(settings, "image_vlm_enabled", True)
    monkeypatch.setattr(settings, "image_vlm_max_per_run", 1)  # default budget tiny…
    async def fake(data, alt, **kw):
        return image_describe.ImageDescription(text="a diagram", kind="diagram")
    monkeypatch.setattr(image_describe, "describe_image", fake)

    src_id, run_id = await _seed_source_run_with_n_images(factory, 3, kind="enrich")
    svc = FirecrawlService()
    async with factory() as db:
        await svc.enrich_source_run(db, src_id, run_id)

    async with factory() as s:
        run = await s.get(ExtractionRun, run_id)
        assert run.status == RunStatus.COMPLETED
        assert run.articles_updated == 3            # …but the enrich run drained ALL 3
        imgs = (await s.execute(select(ArticleImage).where(ArticleImage.article_id.in_(
            select(Article.id).where(Article.source_id == src_id))))).scalars().all()
        assert all(i.description is not None for i in imgs)
        # run_start floor row exists for this run
        rs = (await s.execute(select(ContentChange).where(
            ContentChange.run_id == run_id, ContentChange.change_type == "run_start"))).scalars().all()
        assert len(rs) == 1
        # Article.content_hash was NOT modified by enrichment
        art = (await s.execute(select(Article).where(Article.source_id == src_id))).scalars().first()
        assert art.content_hash == "h-raw"
```

(`_seed_source_run_with_n_images` seeds the source + a `kind="enrich"` `ExtractionRun` (status RUNNING) + n articles with `content_hash="h-raw"` and a meaningful image each on disk; returns `(source_id, run_id)`.)

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend && ./venv/bin/pytest tests/test_enrich_source_run.py -v`
Expected: FAIL — `FirecrawlService` has no `enrich_source_run`.

- [ ] **Step 3: Implement `enrich_source_run`**

In `app/services/firecrawl.py`, add the method (place it near `retry_escalation_run`). `image_describe`, `change_log`, `webhook_dispatcher`, `encode_delta_cursor`, `ContentChange`, `func`, `select`, `SourceStatus`, `RunStatus` are already imported.

```python
    async def enrich_source_run(
        self, db: AsyncSession, source_id: uuid.UUID, run_id: uuid.UUID,
    ) -> ExtractionRun:
        """Worker entrypoint for a kind='enrich' run: describe ALL of a source's
        missing images (no scrape, no per-run budget). Mirrors retry_escalation_run."""
        source = (await db.execute(
            select(DocumentationSource).where(DocumentationSource.id == source_id)
        )).scalar_one()
        run = (await db.execute(
            select(ExtractionRun).where(ExtractionRun.id == run_id)
        )).scalar_one()
        run.current_phase = "image_enrich"
        source.status = SourceStatus.EXTRACTING
        await webhook_dispatcher.prepare_run(db, run_id, source_id)
        # Committed floor so the delta feed withholds this run's mid-run rows.
        await change_log.record_run_start(db, source_id=source_id, run_id=run_id)
        await db.commit()

        # Drain ALL missing images (max_new huge = unlimited).
        described = await image_describe.enrich_run_images(db, source_id, run_id, max_new=10**9)

        now = datetime.now(timezone.utc)
        run = (await db.execute(
            select(ExtractionRun).where(ExtractionRun.id == run_id)
        )).scalar_one()
        run.articles_updated = described
        run.articles_extracted = described
        run.status = RunStatus.COMPLETED
        run.completed_at = now
        source.status = SourceStatus.COMPLETED
        source.last_extracted_at = now
        await db.flush()

        # Nudge the downstream to pull (same delta summary as extract_source).
        if webhook_dispatcher.run_has_subscribers(run_id, "extraction_complete"):
            counts = await change_log.run_change_counts(db, run_id)
            max_seq = (await db.execute(select(func.max(ContentChange.id)))).scalar() or 0
            webhook_dispatcher.spawn_event(
                event_type="extraction_complete", run_id=run_id, source_id=source_id,
                extra={
                    "status": "completed", "articles_extracted": described,
                    "articles_updated": described, "articles_unchanged": 0, "articles_resumed": 0,
                    "delta": {
                        "added": counts["added"], "updated": counts["updated"],
                        "removed": counts["removed"], "watermark": encode_delta_cursor(max_seq),
                    },
                },
            )
        webhook_dispatcher.finish_run(run_id)
        return run
```

- [ ] **Step 4: Wire the worker dispatch**

In `app/worker.py`, extend the kind branch:

```python
                if run_kind == "escalate":
                    await firecrawl_service.retry_escalation_run(db, source_id, run_id)
                elif run_kind == "enrich":
                    await firecrawl_service.enrich_source_run(db, source_id, run_id)
                else:
                    await firecrawl_service.extract_source(db, source_id, run_id=run_id)
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `cd backend && ./venv/bin/pytest tests/test_enrich_source_run.py -v`
Expected: PASS.

- [ ] **Step 6: Regression**

Run: `cd backend && ./venv/bin/pytest tests/test_worker.py tests/test_retry_escalation_route.py tests/test_delta_webhook.py -q`
Expected: pass.

- [ ] **Step 7: Commit**

```bash
git add backend/app/worker.py backend/app/services/firecrawl.py backend/tests/test_enrich_source_run.py
git commit -m "feat(enrich): kind=enrich worker dispatch + enrich_source_run (drain-all, floor, webhook)"
```

---

## Task 4: Delta record serves `sha256(content_markdown)`

**Files:**
- Modify: `app/services/delta_feed.py`
- Test: `tests/test_delta_served_hash.py`

**Interfaces:**
- Produces: delta content records' `content_hash` = `sha256(content_markdown)` (hex), so it reflects the served content (including injected captions).

- [ ] **Step 1: Write the failing test**

Create `tests/test_delta_served_hash.py` (async `httpx.AsyncClient` harness like `tests/test_delta_feed.py`). Seed an article + a `content_changes` `added` row, fetch the bootstrap feed, and assert the record's `content_hash` equals `sha256(content_markdown)` — and differs from the Article's stored `content_hash`:

```python
import hashlib

async def test_delta_record_hash_is_of_served_markdown(ctx):
    c, factory = ctx
    md = "# A\n\n![p](/media/x/y.png)\n\n> **Figure:** A diagram.\n"
    art_id = await _seed_article(factory, content_markdown=md, content_hash="raw-fingerprint")
    resp = await c.get("/api/articles/delta")
    rec = next(r for r in _records(resp.text) if r.get("change_type") == "added")
    assert rec["content_hash"] == hashlib.sha256(md.encode("utf-8")).hexdigest()
    assert rec["content_hash"] != "raw-fingerprint"
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend && ./venv/bin/pytest tests/test_delta_served_hash.py -v`
Expected: FAIL — the record currently returns the Article's `content_hash` (`"raw-fingerprint"`).

- [ ] **Step 3: Implement**

In `app/services/delta_feed.py`: add `import hashlib` at the top. In `_content_record`, change:
```python
        "content_hash": article.content_hash,
```
to:
```python
        # Hash of the SERVED content so a consumer can detect enrichment updates
        # (caption injection changes content_markdown but not the Article's raw
        # content_hash). Purely a serve-time value; the Article row is untouched.
        "content_hash": hashlib.sha256(article.content_markdown.encode("utf-8")).hexdigest(),
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd backend && ./venv/bin/pytest tests/test_delta_served_hash.py -v`
Expected: PASS.

- [ ] **Step 5: Regression**

Run: `cd backend && ./venv/bin/pytest tests/test_delta_feed.py tests/test_image_surfacing.py -q`
Expected: pass (no existing test asserts the record `content_hash` value).

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/delta_feed.py backend/tests/test_delta_served_hash.py
git commit -m "feat(delta): serve sha256(content_markdown) as the record content_hash"
```

---

## Task 5: Enrichment stats — `GET /api/dashboard/enrichment`

**Files:**
- Modify: `app/schemas/dashboard.py`
- Modify: `app/routes/dashboard.py`
- Test: `tests/test_enrichment_stats.py`

**Interfaces:**
- Produces: `GET /api/dashboard/enrichment` → `DashboardEnrichmentResponse { aggregate: {described, pending, sources_with_backlog}, sources: [SourceEnrichmentRow] }`.

- [ ] **Step 1: Add the schemas**

In `app/schemas/dashboard.py`:

```python
class SourceEnrichmentRow(BaseModel):
    source_id: uuid.UUID
    vendor: str
    product: str
    name: str
    described: int
    pending: int
    active_run: bool


class EnrichmentAggregate(BaseModel):
    described: int
    pending: int
    sources_with_backlog: int


class DashboardEnrichmentResponse(BaseModel):
    aggregate: EnrichmentAggregate
    sources: list[SourceEnrichmentRow]
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_enrichment_stats.py` (async `httpx.AsyncClient` harness). Seed two sources: source A with 2 described + 3 pending (mix of `is_meaningful IS NULL` and `is_meaningful=True, description NULL`) + 1 decorative (`is_meaningful=False`); source B fully described. Give source B an active `RUNNING` run. Assert:

```python
async def test_enrichment_stats(ctx, monkeypatch):
    c, factory = ctx
    a_id, b_id = await _seed_two_sources(factory)   # A: 2 described,3 pending,1 decorative ; B: all described + active run
    resp = await c.get("/api/dashboard/enrichment")
    assert resp.status_code == 200
    body = resp.json()
    rows = {r["source_id"]: r for r in body["sources"]}
    assert rows[str(a_id)]["described"] == 2 and rows[str(a_id)]["pending"] == 3  # decorative excluded
    assert rows[str(a_id)]["active_run"] is False
    assert rows[str(b_id)]["pending"] == 0 and rows[str(b_id)]["active_run"] is True
    assert body["aggregate"]["described"] == rows[str(a_id)]["described"] + rows[str(b_id)]["described"]
    assert body["aggregate"]["sources_with_backlog"] == 1  # only A has pending>0
```

- [ ] **Step 3: Run to verify it fails**

Run: `cd backend && ./venv/bin/pytest tests/test_enrichment_stats.py -v`
Expected: FAIL — route not defined (404).

- [ ] **Step 4: Implement the endpoint**

In `app/routes/dashboard.py`, add the import for the new schemas + `ArticleImage`, `Article`, `Product`, `Vendor`, `ExtractionRun`, `RunStatus`, `func`, `or_`, `case`, and the `get_principal`/`Principal` (match how `dashboard_sources` gets its principal). Add:

```python
@router.get("/enrichment", response_model=DashboardEnrichmentResponse)
async def dashboard_enrichment(
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """Per-source and corpus-wide image-enrichment progress."""
    visible = principal.visible_vendor_ids()
    if visible is not None and not visible:
        return DashboardEnrichmentResponse(
            aggregate=EnrichmentAggregate(described=0, pending=0, sources_with_backlog=0), sources=[]
        )

    described_c = func.count().filter(ArticleImage.description.isnot(None))
    pending_c = func.count().filter(
        and_(ArticleImage.description.is_(None), ArticleImage.is_meaningful.isnot(False))
    )
    q = (
        select(
            DocumentationSource.id, DocumentationSource.name,
            Vendor.name.label("vendor"), Product.name.label("product"),
            described_c.label("described"), pending_c.label("pending"),
        )
        .select_from(ArticleImage)
        .join(Article, Article.id == ArticleImage.article_id)
        .join(DocumentationSource, DocumentationSource.id == Article.source_id)
        .join(Product, Product.id == DocumentationSource.product_id)
        .join(Vendor, Vendor.id == Product.vendor_id)
        .group_by(DocumentationSource.id, DocumentationSource.name, Vendor.name, Product.name)
    )
    if visible is not None:
        q = q.where(Product.vendor_id.in_(visible))
    rows = (await db.execute(q)).all()

    active = set((await db.execute(
        select(ExtractionRun.source_id).where(
            ExtractionRun.status.in_([RunStatus.PENDING, RunStatus.RUNNING, RunStatus.PAUSED])
        )
    )).scalars().all())

    out = [
        SourceEnrichmentRow(
            source_id=r.id, vendor=r.vendor, product=r.product, name=r.name,
            described=r.described, pending=r.pending, active_run=r.id in active,
        )
        for r in rows
    ]
    return DashboardEnrichmentResponse(
        aggregate=EnrichmentAggregate(
            described=sum(r.described for r in out),
            pending=sum(r.pending for r in out),
            sources_with_backlog=sum(1 for r in out if r.pending > 0),
        ),
        sources=out,
    )
```

(Add `from sqlalchemy import and_` if not already imported.)

- [ ] **Step 5: Run to verify it passes**

Run: `cd backend && ./venv/bin/pytest tests/test_enrichment_stats.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/routes/dashboard.py backend/app/schemas/dashboard.py backend/tests/test_enrichment_stats.py
git commit -m "feat(enrich): GET /api/dashboard/enrichment per-source + aggregate stats"
```

---

## Task 6: Frontend — types, API client, JobsView label

**Files:**
- Modify: `frontend/src/types/index.ts`, `frontend/src/api/client.ts`, `frontend/src/components/JobsView.tsx`

**Interfaces:**
- Produces: `getEnrichmentStats()`, `enrichSource(id)`, and the `EnrichmentSummary`/`SourceEnrichment` types used by Tasks 7–8.

- [ ] **Step 1: Add types**

In `frontend/src/types/index.ts`:

```typescript
export interface SourceEnrichment {
  source_id: string;
  vendor: string;
  product: string;
  name: string;
  described: number;
  pending: number;
  active_run: boolean;
}

export interface EnrichmentSummary {
  aggregate: { described: number; pending: number; sources_with_backlog: number };
  sources: SourceEnrichment[];
}
```

- [ ] **Step 2: Add client functions**

In `frontend/src/api/client.ts` (follow the existing `export async function` + `api.get/post` pattern; the axios instance already prefixes `/api`):

```typescript
export async function getEnrichmentStats(): Promise<EnrichmentSummary> {
  const res = await api.get("/dashboard/enrichment");
  return res.data;
}

export async function enrichSource(sourceId: string): Promise<{ run_id: string; status: string }> {
  const res = await api.post(`/extraction/enrich/${sourceId}`);
  return res.data;
}
```
(Import `EnrichmentSummary` from `../types` if the file imports types explicitly.)

- [ ] **Step 3: Label enrich runs in JobsView**

Read `frontend/src/components/JobsView.tsx`. Wherever a run's kind is rendered (or add it near the run title/badges), render a small badge when `run.kind === "enrich"` (e.g. `<span className="kind-badge">enrich</span>`), following how `escalate`/escalation is already shown. Keep it minimal and consistent with existing badge styling.

- [ ] **Step 4: Type-check + lint + build**

Run: `cd frontend && npm run lint && npm run build`
Expected: no type or lint errors.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/types/index.ts frontend/src/api/client.ts frontend/src/components/JobsView.tsx
git commit -m "feat(enrich/ui): enrichment types, API client, JobsView enrich label"
```

---

## Task 7: Frontend — SourceList badge + "Describe missing images"

**Files:**
- Modify: `frontend/src/components/SourceList.tsx`

**Interfaces:**
- Consumes: `getEnrichmentStats()`, `enrichSource()` (Task 6).

- [ ] **Step 1: Implement**

Read `frontend/src/components/SourceList.tsx`. It renders `sources.map((s) => ...)` and has a per-source `SourceItem` component. Add:

1. In the parent `SourceList`, fetch `getEnrichmentStats()` on mount (and expose a `refreshEnrichment()` callback), building a `Map<source_id, SourceEnrichment>`. Pass each source's entry down to `SourceItem` (add a prop `enrichment?: SourceEnrichment` and `onEnriched: () => void`).
2. In `SourceItem`, when `enrichment` is present and `described + pending > 0`, render a badge: `🖼 {described}/{described + pending} described` plus, when `pending > 0`, ` · {pending} pending`.
3. When `pending > 0`, render a **"Describe missing images"** button, `disabled={enrichment.active_run}` (title when disabled: "A run is already active"). On click: `await enrichSource(s.id)` → show a success toast/inline message ("Enrichment queued") → call `onEnriched()` (which re-fetches stats and the run list). On a rejected request, surface `err.response?.data?.detail` as the message.

Follow the file's existing button/toast/error patterns and styling. Do not restructure unrelated parts.

- [ ] **Step 2: Type-check + lint + build**

Run: `cd frontend && npm run lint && npm run build`
Expected: clean.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/SourceList.tsx
git commit -m "feat(enrich/ui): per-source enrichment badge + Describe-missing-images action"
```

---

## Task 8: Frontend — Dashboard enrichment section

**Files:**
- Modify: `frontend/src/components/Dashboard.tsx`

**Interfaces:**
- Consumes: `getEnrichmentStats()`, `enrichSource()` (Task 6).

- [ ] **Step 1: Implement**

Read `frontend/src/components/Dashboard.tsx` (it fetches `DashboardResponse`, renders a `tile-row` + a sources table). Add an **Image enrichment** section:

1. Fetch `getEnrichmentStats()` on mount into state (alongside the existing dashboard fetch).
2. Render a rollup line/tile: `{aggregate.described} / {aggregate.described + aggregate.pending} images described` and `{aggregate.sources_with_backlog} sources with a backlog`.
3. Render a backlog list: `sources.filter(s => s.pending > 0)` sorted by `pending` desc — each row shows `vendor / product / name`, `{pending} pending`, and an **Enrich** button (`disabled={active_run}`; on click `enrichSource(source_id)` → toast → refetch stats). Empty state: "All images described." when the backlog is empty.

Match the file's existing tile/table/styling patterns.

- [ ] **Step 2: Type-check + lint + build**

Run: `cd frontend && npm run lint && npm run build`
Expected: clean.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/Dashboard.tsx
git commit -m "feat(enrich/ui): Dashboard image-enrichment rollup + backlog"
```

---

## Task 9: Update project documentation

**Files:**
- Modify: `docs/API.md`, `docs/ARCHITECTURE.md`, `README.md`, `CLAUDE.md`

**Interfaces:** none (docs only). Do this last so it reflects the shipped code.

- [ ] **Step 1: API reference**

In `docs/API.md`:
- **Extraction** section table — add a row: `| POST | /api/extraction/enrich/{source_id} | Queue an image-enrichment-only run (describe all missing images; no re-scrape) |`.
- **Dashboard** section table — add a row: `| GET | /api/dashboard/enrichment | Per-source + corpus image-enrichment progress (described/pending) |`.
- **Delta Feed** section — update the note about `images[].description`/`kind` (already present) and add one line to the record description: "`content_hash` is the SHA-256 of the served `content_markdown` (so it changes when captions/descriptions are injected — safe for consumers to de-dup on)." If the existing Delta Feed text describes `content_hash` as a raw-scrape fingerprint, correct it here.

- [ ] **Step 2: Architecture**

In `docs/ARCHITECTURE.md`:
- In the extraction-flow / run-kinds description, note the third run kind: `enrich` — a scrape-free run that only runs the image-enrichment phase, draining all of a source's missing images (alongside `extract` and the PDF `escalate`).
- In the **Delta Feed** subsection, note that the served record's `content_hash` is `sha256(content_markdown)` (reflects served content, incl. injected captions) — distinct from the Article's internal raw-scrape `content_hash` used for change detection.

- [ ] **Step 3: README**

In `README.md`, extend the VLM-image-descriptions feature bullet (or add a short clause) to mention: on-demand "describe missing images" per source + enrichment-progress monitoring in the UI (Sources list + Dashboard).

- [ ] **Step 4: CLAUDE.md**

In `CLAUDE.md`:
- Under the extraction/run notes, mention the `kind="enrich"` run (`enrich_source_run`, drain-all, no scrape) as the third worker dispatch branch after `extract`/`escalate`.
- Note the delta-feed nuance: the delta record's `content_hash` is `sha256(content_markdown)` (served content), while the Article's `content_hash` remains the raw-scrape fingerprint for change detection — don't conflate them.

- [ ] **Step 5: Sanity check (links/consistency)**

Confirm no broken intra-doc anchors were introduced and the model/endpoint counts you cite are consistent. (Docs only — no build/tests.)

- [ ] **Step 6: Commit**

```bash
git add docs/API.md docs/ARCHITECTURE.md README.md CLAUDE.md
git commit -m "docs: image-enrichment monitoring, on-demand enrich endpoint, served-content delta hash"
```

---

## Self-Review

**Spec coverage:**
- `enrich_run_images` `max_new` drain-all + count → Task 1. ✅
- `POST /api/extraction/enrich/{source_id}` (409s: disabled / nothing-pending / active) → Task 2. ✅
- `kind="enrich"` worker dispatch + `enrich_source_run` (run_start floor, drain-all, counters, `extraction_complete` nudge, `content_hash` untouched) → Task 3. ✅
- Delta record serves `sha256(content_markdown)` → Task 4. ✅
- `GET /api/dashboard/enrichment` (per-source described/pending, decorative excluded, `active_run`, aggregate, RBAC) → Task 5. ✅
- Frontend types/client + JobsView label → Task 6; SourceList badge+button → Task 7; Dashboard rollup+backlog → Task 8. ✅
- Head-of-line note: documented in the spec; no code change (correct). ✅

**Placeholder scan:** Backend steps have complete code + tests. Frontend steps (7–8) describe exact behavior/contracts and instruct reading the component first (the components are large and pattern-heavy; full source isn't reproduced) — verified via `npm run build`/`lint`, since the frontend has no test suite. No TBD/TODO.

**Type consistency:** `enrich_run_images(..., max_new)` return `int` used by `enrich_source_run` (Task 3). `pending` predicate identical across Task 2 (endpoint), Task 5 (stats), and the spec (`description IS NULL AND is_meaningful IS NOT FALSE`). `SourceEnrichment`/`EnrichmentSummary` field names match between backend schema (Task 5) and frontend types (Task 6) — `source_id, vendor, product, name, described, pending, active_run`. `getEnrichmentStats`/`enrichSource` signatures match across Tasks 6–8.
