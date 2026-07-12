# Dashboard Overhaul + PDF-Escalation Stats — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An attention-first Dashboard — one consolidated endpoint feeding a filterable/sortable source table with tiles-as-filters, a flags column, and an in-context drill-down panel — and surface PDF-escalation failure/pending stats there.

**Architecture:** A new `GET /api/dashboard/overview` returns everything per source (status, freshness, articles, last-run, enrichment, escalation, active_run, job) + aggregates. The Dashboard fetches it once (+20s poll) and does all filter/sort/drill-down client-side over ≈280 sources. Escalation is derived from each source's latest run's `escalation_pending`.

**Tech Stack:** FastAPI, SQLAlchemy (async/asyncpg; sync/psycopg2 tests), Pydantic v2, PostgreSQL; React 19 + TypeScript + Vite + Axios.

## Global Constraints

- No DB migration — reuses existing columns (`ExtractionRun.escalation_pending`, `ArticleImage` enrichment cols, `source_type`).
- `pending` images = `description IS NULL AND is_meaningful IS NOT FALSE` (decorative `is_meaningful=false` excluded) — identical to `/api/dashboard/enrichment`.
- Escalation warning = the source's **latest run** has a non-empty `escalation_pending`; `pending_count = len(escalation_pending)`; `run_id` = that run (for the retry action). Only meaningful for `source_type == "pdf"` (web runs never set it).
- `/api/dashboard/sources` and `/api/dashboard/enrichment` **remain** (the latter still backs the `SourceList` badge); `overview` is additive.
- **Tile counts reflect unfiltered totals** (a stable overview); the table reflects composed filters.
- Backend tasks are TDD (`cd backend && ./venv/bin/pytest`). **Frontend has no test suite** — verify with `cd frontend && npm run lint && npm run build`; extract pure helpers (e.g. `filterAndSortRows`) for the non-trivial logic. Read a component fully before editing; follow its conventions.
- Commit trailer on every commit:
  ```
  Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01LZoiNMkURTEexS4UEY8rF4
  ```
- Reference spec: `docs/superpowers/specs/2026-07-11-dashboard-overhaul-design.md`.

---

## File Structure

- `app/schemas/dashboard.py` — **modify** — overview schemas.
- `app/routes/dashboard.py` — **modify** — `GET /api/dashboard/overview`.
- `frontend/src/types/index.ts` — **modify** — overview types.
- `frontend/src/api/client.ts` — **modify** — `getDashboardOverview()`.
- `frontend/src/components/Dashboard.tsx` — **modify** — data swap, filter/sort shell, drill-down panel.
- `frontend/src/components/DashboardDrawer.tsx` — **create** — the drill-down side panel.
- `frontend/src/dashboardView.ts` — **create** — pure `filterAndSortRows` + flag derivation helpers.
- `frontend/src/App.css` — **modify** — filter bar, active-tile, drawer, flags styles.
- Test: `backend/tests/test_dashboard_overview.py`.

---

## Task 1: `GET /api/dashboard/overview` (backend)

**Files:**
- Modify: `app/schemas/dashboard.py`
- Modify: `app/routes/dashboard.py`
- Test: `tests/test_dashboard_overview.py`

**Interfaces:**
- Produces: `GET /api/dashboard/overview` → `DashboardOverviewResponse { aggregate, sources: OverviewSourceRow[] }`.

- [ ] **Step 1: Add schemas**

In `app/schemas/dashboard.py`:

```python
class OverviewLastRun(BaseModel):
    run_id: uuid.UUID
    status: str
    new: int
    updated: int
    unchanged: int


class OverviewEnrichment(BaseModel):
    described: int
    pending: int


class OverviewEscalation(BaseModel):
    warning: bool
    pending_count: int
    run_id: uuid.UUID | None


class OverviewSourceRow(BaseModel):
    id: uuid.UUID
    name: str
    vendor: str
    product: str
    source_type: str
    status: str
    last_extracted_at: str | None
    age_seconds: int | None
    article_count: int
    last_run: OverviewLastRun | None
    enrichment: OverviewEnrichment
    escalation: OverviewEscalation
    active_run: bool
    job_id: uuid.UUID | None
    job_name: str | None
    next_run_at: str | None


class OverviewAggregate(BaseModel):
    total: int
    never_extracted: int
    stale: int
    failing: int
    running: int
    enrichment: EnrichmentAggregate
    escalation_sources_with_warning: int


class DashboardOverviewResponse(BaseModel):
    aggregate: OverviewAggregate
    sources: list[OverviewSourceRow]
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_dashboard_overview.py` (async `httpx.AsyncClient` harness like `tests/test_enrichment_stats.py`; auth disabled → all visible). Seed:
- Source A (web, completed): 2 articles, a latest run with `articles_extracted=3, updated=1, unchanged=200, status=completed`, 2 described + 3 pending images (1 decorative).
- Source B (pdf, completed): latest run with `escalation_pending=[{...},{...},{...}]` (3 segments), no images.
- Source C (web): never extracted (`last_extracted_at=None`), no runs.

```python
async def test_overview_shape_and_signals(ctx):
    c, factory = ctx
    a, b, cid = await _seed_overview(factory)
    resp = await c.get("/api/dashboard/overview")
    assert resp.status_code == 200
    body = resp.json()
    rows = {r["id"]: r for r in body["sources"]}

    ra = rows[str(a)]
    assert ra["source_type"] == "web" and ra["article_count"] == 2
    assert ra["last_run"]["new"] == 3 and ra["last_run"]["updated"] == 1
    assert ra["enrichment"] == {"described": 2, "pending": 3}   # decorative excluded
    assert ra["escalation"]["warning"] is False

    rb = rows[str(b)]
    assert rb["source_type"] == "pdf"
    assert rb["escalation"]["warning"] is True
    assert rb["escalation"]["pending_count"] == 3
    assert rb["escalation"]["run_id"] is not None            # the run to retry
    assert rb["enrichment"] == {"described": 0, "pending": 0}

    rc = rows[str(cid)]
    assert rc["last_extracted_at"] is None and rc["last_run"] is None

    agg = body["aggregate"]
    assert agg["total"] == 3 and agg["never_extracted"] == 1
    assert agg["escalation_sources_with_warning"] == 1
    assert agg["enrichment"]["sources_with_backlog"] == 1     # only A
```

Write `_seed_overview` to create the three sources with the described state (articles, runs incl. `escalation_pending`, and `article_images` with `description`/`is_meaningful` set to produce 2 described + 3 pending + 1 decorative on A).

- [ ] **Step 3: Run to verify it fails**

Run: `cd backend && ./venv/bin/pytest tests/test_dashboard_overview.py -v`
Expected: FAIL — route not defined (404).

- [ ] **Step 4: Implement the endpoint**

In `app/routes/dashboard.py`, add `DashboardOverviewResponse` and the nested schemas to the import, plus `ArticleImage` if not imported. Add the route (it reuses the `dashboard_sources` building blocks — the article-count map, the latest-run-per-source `DISTINCT ON`, the source/vendor/product/job join — plus the enrichment counts and the active-run set):

```python
@router.get("/overview", response_model=DashboardOverviewResponse)
async def dashboard_overview(
    stale_days: int = Query(30, ge=1),
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    now = datetime.now(timezone.utc)
    visible = principal.visible_vendor_ids()
    if visible is not None and not visible:
        return DashboardOverviewResponse(
            aggregate=OverviewAggregate(
                total=0, never_extracted=0, stale=0, failing=0, running=0,
                enrichment=EnrichmentAggregate(described=0, pending=0, sources_with_backlog=0),
                escalation_sources_with_warning=0,
            ),
            sources=[],
        )

    # Article counts (active only).
    counts: dict = {}
    for sid, n in await db.execute(
        select(Article.source_id, func.count())
        .where(Article.removed_at.is_(None)).group_by(Article.source_id)
    ):
        counts[sid] = n

    # Latest run per source (same DISTINCT ON as /sources; PENDING de-prioritised).
    _pending_last = case((ExtractionRun.status == RunStatus.PENDING, 1), else_=0)
    latest_run: dict = {}
    for run in (await db.execute(
        select(ExtractionRun).distinct(ExtractionRun.source_id)
        .order_by(ExtractionRun.source_id, _pending_last, ExtractionRun.started_at.desc())
    )).scalars():
        latest_run[run.source_id] = run

    # Enrichment counts per source (same predicate as /enrichment).
    enr: dict = {}
    described_c = func.count().filter(ArticleImage.description.isnot(None))
    pending_c = func.count().filter(
        and_(ArticleImage.description.is_(None), ArticleImage.is_meaningful.isnot(False))
    )
    for sid, d, p in await db.execute(
        select(Article.source_id, described_c, pending_c)
        .select_from(ArticleImage).join(Article, Article.id == ArticleImage.article_id)
        .group_by(Article.source_id)
    ):
        enr[sid] = (d, p)

    active = set((await db.execute(
        select(ExtractionRun.source_id).where(
            ExtractionRun.status.in_([RunStatus.PENDING, RunStatus.RUNNING, RunStatus.PAUSED])
        )
    )).scalars().all())

    rows_q = (
        select(
            DocumentationSource, Vendor.name.label("vendor"), Product.name.label("product"),
            Job.id.label("job_id"), Job.name.label("job_name"), Job.next_run_at.label("next_run_at"),
        )
        .join(Product, DocumentationSource.product_id == Product.id)
        .join(Vendor, Product.vendor_id == Vendor.id)
        .outerjoin(Job, DocumentationSource.job_id == Job.id)
        .order_by(Vendor.name, Product.name, DocumentationSource.name)
    )
    if visible is not None:
        rows_q = rows_q.where(Product.vendor_id.in_(visible))
    rows = (await db.execute(rows_q)).all()

    out: list[OverviewSourceRow] = []
    total = never = stale = failing = running = esc_warn = 0
    agg_described = agg_pending = backlog = 0
    stale_cutoff = now - timedelta(days=stale_days)

    for src, vendor, product, job_id, job_name, next_run_at in rows:
        total += 1
        last = src.last_extracted_at
        age = int((now - last).total_seconds()) if last else None
        if last is None:
            never += 1
        elif last < stale_cutoff:
            stale += 1
        if src.status == SourceStatus.FAILED:
            failing += 1
        if src.status == SourceStatus.EXTRACTING:
            running += 1

        run = latest_run.get(src.id)
        last_run = OverviewLastRun(
            run_id=run.id, status=run.status.value, new=run.articles_extracted,
            updated=run.articles_updated, unchanged=run.articles_unchanged,
        ) if run else None

        d, p = enr.get(src.id, (0, 0))
        agg_described += d
        agg_pending += p
        if p > 0:
            backlog += 1

        esc_list = (run.escalation_pending if run else None) or []
        escalation = OverviewEscalation(
            warning=bool(esc_list), pending_count=len(esc_list),
            run_id=run.id if esc_list else None,
        )
        if esc_list:
            esc_warn += 1

        out.append(OverviewSourceRow(
            id=src.id, name=src.name, vendor=vendor, product=product,
            source_type=src.source_type, status=src.status.value,
            last_extracted_at=last.isoformat() if last else None, age_seconds=age,
            article_count=counts.get(src.id, 0), last_run=last_run,
            enrichment=OverviewEnrichment(described=d, pending=p),
            escalation=escalation, active_run=src.id in active,
            job_id=job_id, job_name=job_name,
            next_run_at=next_run_at.isoformat() if next_run_at else None,
        ))

    return DashboardOverviewResponse(
        aggregate=OverviewAggregate(
            total=total, never_extracted=never, stale=stale, failing=failing, running=running,
            enrichment=EnrichmentAggregate(
                described=agg_described, pending=agg_pending, sources_with_backlog=backlog),
            escalation_sources_with_warning=esc_warn,
        ),
        sources=out,
    )
```

Ensure `and_` is imported (from Task/existing enrichment route). `src.source_type` is a plain string column.

- [ ] **Step 5: Run to verify it passes**

Run: `cd backend && ./venv/bin/pytest tests/test_dashboard_overview.py -v`
Expected: PASS.

- [ ] **Step 6: Regression + parity**

Run: `cd backend && ./venv/bin/pytest tests/test_dashboard.py tests/test_enrichment_stats.py -q`
Expected: pass (both existing endpoints unchanged).

- [ ] **Step 7: Commit**

```bash
git add backend/app/routes/dashboard.py backend/app/schemas/dashboard.py backend/tests/test_dashboard_overview.py
git commit -m "feat(dashboard): GET /api/dashboard/overview (per-source status/enrichment/escalation + aggregates)"
```

---

## Task 2: Frontend data swap — Dashboard consumes `overview`

**Files:**
- Modify: `frontend/src/types/index.ts`, `frontend/src/api/client.ts`, `frontend/src/components/Dashboard.tsx`

**Interfaces:**
- Produces: `getDashboardOverview()`; `DashboardOverview` types. Dashboard renders its existing table (source, status, freshness, articles, last-run, job) from the new shape — no new interactions yet.

- [ ] **Step 1: Types**

In `frontend/src/types/index.ts`, add interfaces mirroring the backend schema: `OverviewLastRun`, `OverviewEnrichment`, `OverviewEscalation`, `OverviewSourceRow`, `OverviewAggregate`, `DashboardOverview { aggregate, sources }`. Field names/types must match the backend exactly (`described/pending/pending_count/article_count: number`, `active_run/warning: boolean`, ids/strings as string, `run_id: string | null`).

- [ ] **Step 2: Client**

In `frontend/src/api/client.ts`:
```typescript
export async function getDashboardOverview(): Promise<DashboardOverview> {
  const res = await api.get("/dashboard/overview");
  return res.data;
}
```

- [ ] **Step 3: Dashboard data swap**

Read `frontend/src/components/Dashboard.tsx` fully. Replace its data source: fetch `getDashboardOverview()` on mount (+ a 20s poll, matching the pattern already there), store the `DashboardOverview`. Render the **existing** summary tiles from `aggregate` (Sources/Never/Stale/Failing/Running) and the **existing** table columns from `sources` (map old fields: `vendor_name→vendor`, `product_name→product`, `last_run_new→last_run?.new`, etc.). Keep the current health-sort as the default. Preserve the existing image-enrichment rollup (now read from `aggregate.enrichment` + `sources` backlog) OR leave the current `/dashboard/enrichment`-backed section untouched for this task — **do not** remove behavior; just swap the data source cleanly. Keep loading/empty states.

Row-click still navigates to the full source view (as today) — the drawer comes in Task 4.

- [ ] **Step 4: Type-check + lint + build**

Run: `cd frontend && npm run lint && npm run build`
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/types/index.ts frontend/src/api/client.ts frontend/src/components/Dashboard.tsx
git commit -m "feat(dashboard/ui): consume /dashboard/overview (data swap, same table)"
```

---

## Task 3: Filter/sort shell + flags column

**Files:**
- Create: `frontend/src/dashboardView.ts`
- Modify: `frontend/src/components/Dashboard.tsx`, `frontend/src/App.css`

**Interfaces:**
- Produces: pure `filterAndSortRows(rows, filters, sort)` and `rowFlags(row)` in `dashboardView.ts`, consumed by `Dashboard.tsx`.

- [ ] **Step 1: Pure view logic (`dashboardView.ts`)**

Create `frontend/src/dashboardView.ts` with:
- `type Flag = "never" | "stale" | "failed" | "enrichment-backlog" | "escalation-warning" | "running";`
- `rowFlags(row: OverviewSourceRow, staleSeconds: number): Flag[]` — derive from the row (never: `age_seconds === null`; stale: `age_seconds > staleSeconds`; failed: `status === "failed"`; enrichment-backlog: `enrichment.pending > 0`; escalation-warning: `escalation.warning`; running: `active_run`).
- `type DashFilters = { search: string; vendors: string[]; types: string[]; statuses: string[]; flags: Flag[]; tile: string | null }`.
- `type DashSort = { key: string; dir: "asc" | "desc" }`.
- `filterAndSortRows(rows, filters, sort, staleSeconds): OverviewSourceRow[]` — apply: search (case-insensitive over `vendor/product/name`), vendor/type/status facets (OR within a facet, AND across facets), flags multi-select (row must have all selected flags), the active `tile` (maps a tile id → a flag or predicate), then sort by `key` (`name` = `${vendor}${product}${name}`, `freshness` = `age_seconds` nulls-first for asc, `articles`, `last_run` status, `pending` = `enrichment.pending`, `escalation` = `escalation.pending_count`) in `dir`. Default (no explicit sort) = attention-first: never → failed → stale → escalation-warning → rest, then name.

Keep this file free of React/DOM so it's trivially correct and unit-testable if a runner is added.

- [ ] **Step 2: Wire the shell into `Dashboard.tsx`**

Add state: `search`, `vendors`, `types`, `statuses`, `flags`, `tile`, `sort`. Compute the visible rows via `useMemo(() => filterAndSortRows(data.sources, {…}, sort, staleSeconds), [data, …])`. Render:
- **Tiles** (Sources/Stale/Failing/Running/Enrichment/Escalation) as **toggle filters**: clicking sets/clears `tile`; the active tile gets an `active` class; counts come from `aggregate` (unfiltered). (Sources tile clears all filters.)
- A **filter bar**: `Vendor` multi-select (options = distinct vendors from rows), `Type` (web/pdf), `Status`, `Flags` multi-select, and a **search** input. An "X active filters · Clear" affordance.
- The **table** with **sortable headers** (click toggles/sets `sort`), and a new **Flags** column rendering `rowFlags(row)` as compact badges (`stale`, `failed`, `🖼N`, `⚠pdf(N)`, `▶`).

Add minimal CSS to `App.css` for the filter bar, active-tile state, and flag badges (reuse existing tile/badge tokens).

- [ ] **Step 3: Type-check + lint + build**

Run: `cd frontend && npm run lint && npm run build`
Expected: clean.

- [ ] **Step 4: Manual sanity**

Verify (describe in the report): tile toggle filters the table; facets compose; search narrows; column sort works; flags render.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/dashboardView.ts frontend/src/components/Dashboard.tsx frontend/src/App.css
git commit -m "feat(dashboard/ui): tiles-as-filters, facets, search, sortable columns, flags"
```

---

## Task 4: In-context drill-down side panel

**Files:**
- Create: `frontend/src/components/DashboardDrawer.tsx`
- Modify: `frontend/src/components/Dashboard.tsx`, `frontend/src/App.css`

**Interfaces:**
- Consumes: `listRuns(sourceId)`, `enrichSource(sourceId)`, `retryEscalation(runId)`, `getSource(id)`, `apiError`.

- [ ] **Step 1: Build the drawer**

Create `frontend/src/components/DashboardDrawer.tsx` — props `{ row: OverviewSourceRow; onClose: () => void; onAction: () => void }`. Render a right-side panel (fixed, right, with a backdrop; ESC and backdrop-click call `onClose`):
- **Header**: `vendor › product › name`, status, type; an "Open full source view" button (`getSource(row.id)` → the app's source-select handler, threaded from `Dashboard` via a prop).
- **Runs**: `listRuns(row.id)` on open → a compact recent-runs list (status, counts, started_at; link to logs if trivial).
- **Image enrichment**: `{described} / {described+pending}`, and a **Describe missing images** button (`enrichSource(row.id)`; disabled when `row.active_run || row.enrichment.pending === 0`). On success → toast + `onAction()`.
- **PDF escalation** (only when `row.source_type === "pdf" && row.escalation.warning`): `{pending_count} segments pending` + a **Retry escalation** button (`retryEscalation(row.escalation.run_id!)`; disabled when `row.active_run`). On success → toast + `onAction()`.
- Errors via `apiError(e, …)`.

- [ ] **Step 2: Wire into Dashboard**

Add `selected: OverviewSourceRow | null` state. Row-click sets `selected` (instead of navigating). Render `{selected && <DashboardDrawer row={selected} onClose={() => setSelected(null)} onAction={reloadOverview} … />}`. `onAction`/`reloadOverview` re-fetches the overview so tiles/table/drawer stay consistent. Keep a way to reach the full source view (the drawer's "Open full source view" button).

Add CSS for the drawer + backdrop (responsive: full-width overlay under a narrow breakpoint).

- [ ] **Step 3: Type-check + lint + build**

Run: `cd frontend && npm run lint && npm run build`
Expected: clean.

- [ ] **Step 4: Manual sanity**

Verify (in the report): row-click opens the panel; runs load; the enrich button gates on pending/active and triggers + refreshes; the retry-escalation button shows for a pdf-with-warning source and triggers + refreshes; ESC/backdrop closes.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/DashboardDrawer.tsx frontend/src/components/Dashboard.tsx frontend/src/App.css
git commit -m "feat(dashboard/ui): in-context drill-down drawer (runs, enrich, retry-escalation)"
```

---

## Task 5: Documentation

**Files:**
- Modify: `docs/API.md`, `docs/ARCHITECTURE.md`

- [ ] **Step 1: API.md**

Under **Dashboard**, add: `| GET | /api/dashboard/overview | Consolidated per-source overview (status, freshness, articles, last-run, enrichment, escalation, active_run, job) + aggregates; powers the Dashboard's filter/sort/drill-down |`. Note that `/dashboard/sources` and `/dashboard/enrichment` remain (the latter backs the per-source badge in the Sources list).

- [ ] **Step 2: ARCHITECTURE.md**

In the frontend/UI notes, add a short "Dashboard" paragraph: attention-first — one `overview` fetch (+20s poll), client-side filter/sort/drill-down, tiles-as-filters, and a side-panel that consolidates runs + enrichment + PDF-escalation retry. Mention the escalation signal comes from the latest run's `escalation_pending`.

- [ ] **Step 3: Commit**

```bash
git add docs/API.md docs/ARCHITECTURE.md
git commit -m "docs: dashboard overview endpoint + attention-first dashboard"
```

---

## Self-Review

**Spec coverage:**
- Consolidated `/api/dashboard/overview` (per-source everything + aggregates), enrichment folded in, escalation signal → Task 1. ✅
- Escalation surfaced (warning/count/run_id + aggregate + drawer retry) → Task 1 + Task 4. ✅
- Data swap (de-risk) → Task 2. ✅
- Tiles-as-filters, facets, search, sortable columns, flags column → Task 3 (pure `filterAndSortRows`/`rowFlags` + wiring). ✅
- In-context side-panel drill-down with enrich + retry actions, refresh-on-action → Task 4. ✅
- `/sources` + `/enrichment` kept; tiles reflect unfiltered totals → Task 1 + Task 3. ✅
- Docs → Task 5. ✅

**Placeholder scan:** Backend task has complete code + a full test. Frontend tasks specify exact contracts (types, client fns, pure-helper signatures, behavior + gating) and extract the non-trivial logic into a pure, testable `dashboardView.ts`; they're verified via `npm run lint`/`build` + a described manual pass (no frontend test runner). No TBD/TODO.

**Type consistency:** `OverviewSourceRow` / nested types field names match between backend schema (Task 1) and frontend types (Task 2), and are consumed unchanged in Tasks 3–4. The `pending` predicate (`description IS NULL AND is_meaningful IS NOT FALSE`) is identical to `/dashboard/enrichment`. Escalation `run_id` (nullable) flows from Task 1 → the drawer's `retryEscalation(run_id!)` (Task 4), guarded by `escalation.warning`. `filterAndSortRows`/`rowFlags` signatures are stable across Tasks 3–4.
