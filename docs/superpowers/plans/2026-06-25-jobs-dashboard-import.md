# Jobs/Dashboard/Import Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix incremental-run progress tracking, add a source picker to the job view, add a health-first dashboard, and add CSV bulk import of sources.

**Architecture:** FastAPI + SQLAlchemy async backend with React/TypeScript frontend. Backend adds one column + migration (progress fix), three new endpoints (pickable sources, bulk job-assign, dashboard, CSV import) and exposes a new run counter. Frontend adds `processed`-based progress math, a `SourcePicker`, a `Dashboard` view, and a `BulkImport` panel.

**Tech Stack:** FastAPI, SQLAlchemy (async asyncpg), Alembic, Pydantic v2, React 19, TypeScript, Vite, Axios, pytest + httpx.AsyncClient.

## Global Constraints

- Backend settings prefix is `DOCEXTRACTOR_`; tests run against the `docextractor_test` database (`settings.database_url.rsplit("/",1)[0] + "/docextractor_test"`).
- All new models must be imported in `app/models/__init__.py` (N/A here — no new models, only a column).
- Route tests follow `tests/test_job_routes.py`: `pytest.mark.asyncio`, `httpx.AsyncClient` with `ASGITransport(app=app)`, a per-test `client` fixture that drops+creates `Base.metadata` and overrides `get_db`.
- Counter columns use `Integer, default=0, server_default="0", nullable=False`.
- Alembic current head is `9ad7b7dc0fc7`; the new migration's `down_revision` must be `"9ad7b7dc0fc7"`.
- Frontend has no unit-test runner; frontend verification is `cd frontend && npm run build` (tsc type-check + Vite build) and `npm run lint`.
- All commands below run from `backend/` unless a path says `frontend/`.
- Commit messages: imperative subject line; no specific trailer required by this plan.

---

## Task 1: Add `articles_resumed` column to ExtractionRun

**Files:**
- Modify: `app/models/extraction_run.py` (after `articles_updated`, ~line 79)
- Create: `alembic/versions/a1b2c3d4e5f6_add_articles_resumed.py`
- Test: `tests/test_articles_resumed_model.py`

**Interfaces:**
- Produces: `ExtractionRun.articles_resumed: int` (default 0), used by Tasks 2, 3.

- [ ] **Step 1: Write the failing test**

Create `tests/test_articles_resumed_model.py`:

```python
"""ExtractionRun.articles_resumed column defaults to 0."""
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


async def test_articles_resumed_defaults_to_zero(factory):
    async with factory() as s:
        v = Vendor(name="V"); s.add(v); await s.flush()
        p = Product(vendor_id=v.id, name="P"); s.add(p); await s.flush()
        src = DocumentationSource(product_id=p.id, name="D", base_url="https://d")
        s.add(src); await s.flush()
        run = ExtractionRun(source_id=src.id)
        s.add(run); await s.commit()
        assert run.articles_resumed == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_articles_resumed_model.py -v`
Expected: FAIL — `AttributeError: ... 'ExtractionRun' object has no attribute 'articles_resumed'` (or `TypeError` on the kwarg).

- [ ] **Step 3: Add the column to the model**

In `app/models/extraction_run.py`, directly after the `articles_updated` mapped column (~line 79), add:

```python
    articles_resumed: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_articles_resumed_model.py -v`
Expected: PASS

- [ ] **Step 5: Create the Alembic migration**

Create `alembic/versions/a1b2c3d4e5f6_add_articles_resumed.py`:

```python
"""add extraction_runs.articles_resumed

Counts pages carried over from a prior interrupted attempt (resume checkpoint),
kept separate from the new/updated/unchanged breakdown.

Revision ID: a1b2c3d4e5f6
Revises: 9ad7b7dc0fc7
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "9ad7b7dc0fc7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "extraction_runs",
        sa.Column(
            "articles_resumed",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("extraction_runs", "articles_resumed")
```

- [ ] **Step 6: Commit**

```bash
git add app/models/extraction_run.py alembic/versions/a1b2c3d4e5f6_add_articles_resumed.py tests/test_articles_resumed_model.py
git commit -m "feat(runs): add articles_resumed counter column"
```

---

## Task 2: Use `articles_resumed` for resume seeding + fix blocked-run guard

**Files:**
- Modify: `app/services/firecrawl.py` (raw-HTTP resume ~line 947-952; batch resume ~line 1410-1412; blocked guard ~line 1460)
- Test: `tests/test_resume_counter.py`

**Interfaces:**
- Consumes: `ExtractionRun.articles_resumed` (Task 1).
- Produces: on resume, resumed pages land in `articles_resumed`, not `articles_extracted`; blocked-run guard counts `extracted+updated+unchanged+resumed`.

**Context:** There are two resume points. The Firecrawl-batch path (~line 1410) does `run.articles_resumed = resumed` via the in-memory `run` object. The raw-HTTP path (~line 947) issues a SQL `UPDATE`. The blocked-run guard (~line 1450-1461) re-reads counters and fails a run that persisted nothing.

- [ ] **Step 1: Write the failing test**

Create `tests/test_resume_counter.py`. This test exercises the blocked-guard arithmetic directly (the resume seeding is covered by reading the lines; the guard is the testable unit). It asserts that a run with only resumed pages is NOT treated as a zero/blocked run.

```python
"""Resume bookkeeping: resumed pages count toward 'persisted' for the blocked guard."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.firecrawl import persisted_count


def test_persisted_includes_resumed():
    # extracted, updated, unchanged, resumed
    assert persisted_count(0, 0, 0, 5) == 5


def test_persisted_zero_when_all_zero():
    assert persisted_count(0, 0, 0, 0) == 0


def test_persisted_sums_all_buckets():
    assert persisted_count(2, 3, 10, 5) == 20
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_resume_counter.py -v`
Expected: FAIL — `ImportError: cannot import name 'persisted_count'`.

- [ ] **Step 3: Add the `persisted_count` helper**

In `app/services/firecrawl.py`, add a module-level helper near the top (after imports, before the class), so the arithmetic is testable and reused:

```python
def persisted_count(extracted: int, updated: int, unchanged: int, resumed: int) -> int:
    """Total pages accounted for in a run: freshly processed this attempt plus
    pages carried over from a resumed checkpoint."""
    return (extracted or 0) + (updated or 0) + (unchanged or 0) + (resumed or 0)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_resume_counter.py -v`
Expected: PASS

- [ ] **Step 5: Wire the helper + resumed seeding into the service**

a) Raw-HTTP resume path (~line 947-952). Replace the `UPDATE ... articles_extracted=completed` with `articles_resumed=completed`:

```python
            await db.execute(
                update(ExtractionRun)
                .where(ExtractionRun.id == run_id)
                .values(articles_resumed=completed)
            )
            await db.commit()
```

b) Firecrawl-batch resume path (~line 1410-1412). Change the seeded counter:

```python
                    # Reflect prior progress in the run's counter for an accurate bar.
                    run.articles_resumed = resumed
                    await db.commit()
```

c) Blocked-run guard (~line 1450-1461). Read `articles_resumed` too and use the helper:

```python
            extracted, updated, unchanged, resumed, err = (
                await db.execute(
                    select(
                        ExtractionRun.articles_extracted,
                        ExtractionRun.articles_updated,
                        ExtractionRun.articles_unchanged,
                        ExtractionRun.articles_resumed,
                        ExtractionRun.error_message,
                    ).where(ExtractionRun.id == run.id)
                )
            ).one()
            persisted = persisted_count(extracted, updated, unchanged, resumed)
            if persisted == 0 and err == _BLOCKED_MSG:
```

- [ ] **Step 6: Run the focused tests + the existing resume/raw-http tests**

Run: `pytest tests/test_resume_counter.py tests/test_raw_http_content_engine.py -v`
Expected: PASS (no regressions in the raw-http engine test).

- [ ] **Step 7: Commit**

```bash
git add app/services/firecrawl.py tests/test_resume_counter.py
git commit -m "fix(runs): seed resumed pages into articles_resumed, not articles_extracted"
```

---

## Task 3: Expose `articles_resumed` in extraction run endpoints

**Files:**
- Modify: `app/routes/extraction.py` (`list_runs` dict ~line 225-228; `get_run_status` ~line 152-178)
- Test: `tests/test_runs_resumed_field.py`

**Interfaces:**
- Consumes: `ExtractionRun.articles_resumed` (Task 1).
- Produces: `/api/extraction/runs` and `/api/extraction/runs/{run_id}` include `articles_resumed`. Consumed by Task 4 (frontend).

- [ ] **Step 1: Read `get_run_status` to find the response shape**

Run: `sed -n '152,178p' app/routes/extraction.py`
Note whether it returns the ORM object directly or a dict. If it returns the ORM `run` object (FastAPI serializes via the model's attributes) the field may already serialize; if it builds a dict, add the key. Apply the matching change in Step 3.

- [ ] **Step 2: Write the failing test**

Create `tests/test_runs_resumed_field.py`:

```python
"""/api/extraction/runs exposes articles_resumed."""
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
from app.models import Vendor, Product, DocumentationSource, ExtractionRun

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def client():
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


async def _run(factory) -> uuid.UUID:
    async with factory() as s:
        v = Vendor(name="V"); s.add(v); await s.flush()
        p = Product(vendor_id=v.id, name="P"); s.add(p); await s.flush()
        src = DocumentationSource(product_id=p.id, name="D", base_url="https://d")
        s.add(src); await s.flush()
        run = ExtractionRun(source_id=src.id, articles_resumed=7, articles_total=10)
        s.add(run); await s.commit()
        return run.id


async def test_list_runs_includes_articles_resumed(client):
    c, factory = client
    rid = await _run(factory)
    body = (await c.get("/api/extraction/runs")).json()
    row = next(r for r in body["runs"] if r["id"] == str(rid))
    assert row["articles_resumed"] == 7


async def test_run_status_includes_articles_resumed(client):
    c, factory = client
    rid = await _run(factory)
    body = (await c.get(f"/api/extraction/runs/{rid}")).json()
    assert body["articles_resumed"] == 7
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_runs_resumed_field.py -v`
Expected: FAIL — `KeyError: 'articles_resumed'` (list test), and the status test fails if `get_run_status` builds a dict without the field.

- [ ] **Step 4: Add the field to the route(s)**

In `list_runs` (~line 225, in the per-run dict, next to `articles_unchanged`):

```python
                "articles_unchanged": r.articles_unchanged,
                "articles_resumed": r.articles_resumed,
```

In `get_run_status`: if it builds a dict, add `"articles_resumed": run.articles_resumed,` alongside the other `articles_*` keys. If it returns the ORM object directly and the list test passed for status too, no change is needed there.

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_runs_resumed_field.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/routes/extraction.py tests/test_runs_resumed_field.py
git commit -m "feat(runs): expose articles_resumed in run endpoints"
```

---

## Task 4: Frontend — processed-based progress math

**Files:**
- Modify: `frontend/src/types/index.ts` (`ExtractionRun` type)
- Modify: `frontend/src/components/JobsView.tsx` (`pctOf`, Active list counter, RunDetail stat grid)
- Test: `cd frontend && npm run build` (type-check) — no unit-test runner.

**Interfaces:**
- Consumes: `articles_resumed` from the run API (Task 3).

- [ ] **Step 1: Add `articles_resumed` to the ExtractionRun type**

In `frontend/src/types/index.ts`, find the `ExtractionRun` type and add (next to `articles_unchanged`):

```typescript
  articles_resumed: number;
```

If `articles_unchanged`/`articles_updated` are declared optional (`?`), match that style for `articles_resumed`.

- [ ] **Step 2: Add a `processed` helper and update `pctOf` in JobsView.tsx**

Replace the existing `pctOf` (lines ~48-51) with:

```typescript
function processed(run: ExtractionRun): number {
  return (
    (run.articles_extracted ?? 0) +
    (run.articles_updated ?? 0) +
    (run.articles_unchanged ?? 0) +
    (run.articles_resumed ?? 0)
  );
}

function pctOf(run: ExtractionRun): number | null {
  if (!run.articles_total || run.articles_total <= 0) return null;
  return Math.min(100, Math.round((processed(run) / run.articles_total) * 100));
}
```

- [ ] **Step 3: Use `processed(run)` in the Active list counter**

In the Active list (lines ~154-157), replace `run.articles_extracted` in the "X / Y articles" line with `processed(run)`:

```tsx
                  <span className="sub">
                    {processed(run)} / {run.articles_total || "?"} articles
                    {pct !== null ? ` (${pct}%)` : ""}
                  </span>
```

- [ ] **Step 4: Update RunDetail "Processed / total" + add Carried-over stat**

In `RunDetail` stat grid (lines ~344-347), change the "Processed / total" value to `processed(run)` and add a Carried-over line when `articles_resumed > 0`:

```tsx
            <div><dt>Processed / total</dt><dd>{processed(run)} / {run.articles_total || "?"}</dd></div>
            <div><dt>New</dt><dd>{run.articles_extracted}</dd></div>
            <div><dt>Updated</dt><dd>{run.articles_updated ?? 0}</dd></div>
            <div><dt>Unchanged</dt><dd>{run.articles_unchanged ?? 0}</dd></div>
            {(run.articles_resumed ?? 0) > 0 && (
              <div><dt>Carried over</dt><dd>{run.articles_resumed}</dd></div>
            )}
```

- [ ] **Step 5: Type-check + lint**

Run: `cd frontend && npm run build && npm run lint`
Expected: build succeeds, no type errors.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/types/index.ts frontend/src/components/JobsView.tsx
git commit -m "fix(ui): count updated/unchanged/resumed pages in run progress"
```

---

## Task 5: Backend — `GET /api/sources/pickable`

**Files:**
- Modify: `app/schemas/source.py` (add `PickableSource`, `PickableSourceList`)
- Modify: `app/routes/sources.py` (add route + imports for `Job`, `Vendor`)
- Test: `tests/test_sources_pickable.py`

**Interfaces:**
- Produces: `GET /api/sources/pickable` → `{ "sources": [ {id, name, vendor_name, product_name, job_id, job_name} ] }`. Consumed by Task 7.

**Note:** Declare this route BEFORE `@router.get("/{source_id}")` so the literal `/pickable` path is not captured by the `{source_id}` UUID route.

- [ ] **Step 1: Write the failing test**

Create `tests/test_sources_pickable.py`:

```python
"""GET /api/sources/pickable returns labelled sources with current job."""
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
from app.models.job import Job

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def client():
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


async def test_pickable_lists_sources_with_labels_and_job(client):
    c, factory = client
    async with factory() as s:
        v = Vendor(name="Acme"); s.add(v); await s.flush()
        p = Product(vendor_id=v.id, name="Cloud"); s.add(p); await s.flush()
        job = Job(name="Nightly"); s.add(job); await s.flush()
        s.add(DocumentationSource(product_id=p.id, name="Guide",
                                  base_url="https://d/1", job_id=job.id))
        s.add(DocumentationSource(product_id=p.id, name="API",
                                  base_url="https://d/2"))
        await s.commit()

    body = (await c.get("/api/sources/pickable")).json()
    rows = {r["name"]: r for r in body["sources"]}
    assert rows["Guide"]["vendor_name"] == "Acme"
    assert rows["Guide"]["product_name"] == "Cloud"
    assert rows["Guide"]["job_name"] == "Nightly"
    assert rows["API"]["job_id"] is None
    assert rows["API"]["job_name"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_sources_pickable.py -v`
Expected: FAIL — 404 (route not found) so `body["sources"]` raises `KeyError`.

- [ ] **Step 3: Add the schemas**

In `app/schemas/source.py`, add:

```python
class PickableSource(BaseModel):
    id: uuid.UUID
    name: str
    vendor_name: str
    product_name: str
    job_id: uuid.UUID | None
    job_name: str | None


class PickableSourceList(BaseModel):
    sources: list[PickableSource]
```

- [ ] **Step 4: Add the route**

In `app/routes/sources.py`, add the imports at the top (with the other model imports):

```python
from app.models.job import Job
from app.models.vendor import Vendor
```

and add the import of the new schemas to the existing `from app.schemas.source import (...)` block:

```python
from app.schemas.source import (
    SourceCreate,
    SourceUpdate,
    SourceResponse,
    SourceListResponse,
    PickableSource,
    PickableSourceList,
)
```

Then add this route immediately ABOVE the `@router.get("/{source_id}", ...)` definition:

```python
@router.get("/pickable", response_model=PickableSourceList)
async def list_pickable_sources(db: AsyncSession = Depends(get_db)):
    """All sources with vendor/product labels and their current job (if any),
    for the job view's source picker."""
    rows = (
        await db.execute(
            select(
                DocumentationSource.id,
                DocumentationSource.name,
                Vendor.name.label("vendor_name"),
                Product.name.label("product_name"),
                DocumentationSource.job_id,
                Job.name.label("job_name"),
            )
            .join(Product, DocumentationSource.product_id == Product.id)
            .join(Vendor, Product.vendor_id == Vendor.id)
            .outerjoin(Job, DocumentationSource.job_id == Job.id)
            .order_by(Vendor.name, Product.name, DocumentationSource.name)
        )
    ).all()
    return PickableSourceList(sources=[
        PickableSource(
            id=r.id, name=r.name, vendor_name=r.vendor_name,
            product_name=r.product_name, job_id=r.job_id, job_name=r.job_name,
        )
        for r in rows
    ])
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_sources_pickable.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/schemas/source.py app/routes/sources.py tests/test_sources_pickable.py
git commit -m "feat(sources): add pickable sources endpoint for job picker"
```

---

## Task 6: Backend — bulk `PUT /api/jobs/{job_id}/sources`

**Files:**
- Modify: `app/schemas/job.py` (add `JobSourcesAssign`)
- Modify: `app/routes/jobs.py` (add bulk-assign route + import)
- Test: `tests/test_job_bulk_assign.py`

**Interfaces:**
- Consumes: existing `_response`, `_load` helpers in `jobs.py`.
- Produces: `PUT /api/jobs/{job_id}/sources` body `{ "source_ids": [...] }` → `JobResponse`. Consumed by Task 7.

**Note:** The existing single-source route is `@router.put("/{job_id}/sources/{source_id}")`. The new collection route `@router.put("/{job_id}/sources")` has a distinct path (no trailing id) so they don't collide. Place the new route directly above the single-source `assign_source`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_job_bulk_assign.py`:

```python
"""PUT /api/jobs/{id}/sources assigns/reassigns many sources at once."""
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
async def client():
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


async def _source(factory, name) -> uuid.UUID:
    sfx = uuid.uuid4().hex[:8]
    async with factory() as s:
        v = Vendor(name=f"V-{sfx}"); s.add(v); await s.flush()
        p = Product(vendor_id=v.id, name=f"P-{sfx}"); s.add(p); await s.flush()
        src = DocumentationSource(product_id=p.id, name=name, base_url=f"https://d/{sfx}")
        s.add(src); await s.commit()
        return src.id


async def test_bulk_assign_multiple_sources(client):
    c, factory = client
    s1 = await _source(factory, "A")
    s2 = await _source(factory, "B")
    job = (await c.post("/api/jobs", json={"name": "J"})).json()

    resp = await c.put(f"/api/jobs/{job['id']}/sources",
                       json={"source_ids": [str(s1), str(s2)]})
    assert resp.status_code == 200
    assert resp.json()["source_count"] == 2


async def test_bulk_assign_reassigns_from_other_job(client):
    c, factory = client
    s1 = await _source(factory, "A")
    j1 = (await c.post("/api/jobs", json={"name": "J1"})).json()
    j2 = (await c.post("/api/jobs", json={"name": "J2"})).json()
    await c.put(f"/api/jobs/{j1['id']}/sources", json={"source_ids": [str(s1)]})
    await c.put(f"/api/jobs/{j2['id']}/sources", json={"source_ids": [str(s1)]})

    assert (await c.get(f"/api/jobs/{j1['id']}")).json()["source_count"] == 0
    assert (await c.get(f"/api/jobs/{j2['id']}")).json()["source_count"] == 1


async def test_bulk_assign_unknown_source_is_404(client):
    c, _ = client
    job = (await c.post("/api/jobs", json={"name": "J"})).json()
    resp = await c.put(f"/api/jobs/{job['id']}/sources",
                       json={"source_ids": [str(uuid.uuid4())]})
    assert resp.status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_job_bulk_assign.py -v`
Expected: FAIL — the collection PUT returns 405/422 (no matching route) rather than 200/404.

- [ ] **Step 3: Add the schema**

In `app/schemas/job.py`, add:

```python
class JobSourcesAssign(BaseModel):
    source_ids: list[uuid.UUID]
```

- [ ] **Step 4: Add the bulk-assign route**

In `app/routes/jobs.py`, add `JobSourcesAssign` to the `from app.schemas.job import (...)` block, and `from sqlalchemy import select` already exists. Add this route directly ABOVE `assign_source`:

```python
@router.put("/{job_id}/sources", response_model=JobResponse)
async def assign_sources(
    job_id: uuid.UUID, body: JobSourcesAssign, db: AsyncSession = Depends(get_db)
):
    """Assign (or reassign) many sources to this job at once."""
    job = await _load(db, job_id)
    if body.source_ids:
        found = (
            await db.execute(
                select(DocumentationSource).where(
                    DocumentationSource.id.in_(body.source_ids)
                )
            )
        ).scalars().all()
        if len(found) != len(set(body.source_ids)):
            raise HTTPException(status_code=404, detail="One or more sources not found")
        for src in found:
            src.job_id = job.id
        await db.commit()
    return await _response(db, job)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_job_bulk_assign.py tests/test_job_routes.py -v`
Expected: PASS (existing job route tests still pass — no collision).

- [ ] **Step 6: Commit**

```bash
git add app/schemas/job.py app/routes/jobs.py tests/test_job_bulk_assign.py
git commit -m "feat(jobs): bulk-assign sources to a job"
```

---

## Task 7: Frontend — SourcePicker in the job view

**Files:**
- Modify: `frontend/src/types/index.ts` (add `PickableSource`)
- Modify: `frontend/src/api/client.ts` (add `listPickableSources`, `assignSourcesToJob`)
- Create: `frontend/src/components/SourcePicker.tsx`
- Modify: `frontend/src/components/JobsManager.tsx` (add "Add sources" button + picker to `JobCard`)
- Test: `cd frontend && npm run build` + `npm run lint`

**Interfaces:**
- Consumes: `GET /api/sources/pickable` (Task 5), `PUT /api/jobs/{id}/sources` (Task 6).

- [ ] **Step 1: Add the `PickableSource` type**

In `frontend/src/types/index.ts` add:

```typescript
export interface PickableSource {
  id: string;
  name: string;
  vendor_name: string;
  product_name: string;
  job_id: string | null;
  job_name: string | null;
}
```

- [ ] **Step 2: Add client functions**

In `frontend/src/api/client.ts`, add `PickableSource` and `Job` (already imported) to the type imports, then add:

```typescript
export async function listPickableSources(): Promise<PickableSource[]> {
  const res = await api.get<{ sources: PickableSource[] }>("/sources/pickable");
  return res.data.sources;
}

export async function assignSourcesToJob(
  jobId: string,
  sourceIds: string[],
): Promise<Job> {
  const res = await api.put<Job>(`/jobs/${jobId}/sources`, { source_ids: sourceIds });
  return res.data;
}
```

Add `PickableSource` to the `import type { ... } from "../types";` block at the top.

- [ ] **Step 3: Create the SourcePicker component**

Create `frontend/src/components/SourcePicker.tsx`:

```tsx
import { useEffect, useMemo, useState } from "react";
import type { PickableSource } from "../types";
import { listPickableSources, assignSourcesToJob } from "../api/client";

export default function SourcePicker({
  jobId,
  onClose,
  onAssigned,
}: {
  jobId: string;
  onClose: () => void;
  onAssigned: () => void;
}) {
  const [sources, setSources] = useState<PickableSource[]>([]);
  const [filter, setFilter] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    listPickableSources()
      .then(setSources)
      .catch(() => setError("Failed to load sources"));
  }, []);

  const visible = useMemo(() => {
    const q = filter.trim().toLowerCase();
    return sources.filter((s) =>
      !q ||
      `${s.vendor_name} ${s.product_name} ${s.name}`.toLowerCase().includes(q),
    );
  }, [sources, filter]);

  const toggle = (id: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  };

  const assign = async () => {
    if (selected.size === 0) return;
    setSaving(true);
    setError("");
    try {
      await assignSourcesToJob(jobId, [...selected]);
      onAssigned();
      onClose();
    } catch {
      setError("Failed to assign sources");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="picker-backdrop" onClick={onClose}>
      <div className="picker-panel" onClick={(e) => e.stopPropagation()}>
        <h3>Add sources</h3>
        {error && <div className="error">{error}</div>}
        <input
          type="text"
          placeholder="Filter by vendor, product or source…"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
        />
        <ul className="picker-list">
          {visible.map((s) => (
            <li key={s.id}>
              <label>
                <input
                  type="checkbox"
                  checked={selected.has(s.id)}
                  onChange={() => toggle(s.id)}
                />
                <span>{[s.vendor_name, s.product_name, s.name].join(" › ")}</span>
                {s.job_id && s.job_id !== jobId && (
                  <span className="sub"> (in: {s.job_name})</span>
                )}
                {s.job_id === jobId && <span className="sub"> (already here)</span>}
              </label>
            </li>
          ))}
          {visible.length === 0 && <li className="sub">No sources match.</li>}
        </ul>
        <div className="picker-actions">
          <button className="btn-secondary-sm" onClick={onClose}>Cancel</button>
          <button
            className="btn-primary-sm"
            disabled={saving || selected.size === 0}
            onClick={assign}
          >
            {saving ? "Assigning…" : `Assign ${selected.size || ""}`.trim()}
          </button>
        </div>
      </div>
    </div>
  );
}
```

- [ ] **Step 4: Wire the picker into JobCard**

In `frontend/src/components/JobsManager.tsx`:

a) Add the import at the top:

```typescript
import SourcePicker from "./SourcePicker";
```

b) In `JobCard`, add state near the other `useState` hooks:

```typescript
  const [showPicker, setShowPicker] = useState(false);
```

c) In the `item-actions` div (next to the "Run now" button), add:

```tsx
        <button className="btn-secondary-sm" onClick={() => setShowPicker(true)}>
          Add sources
        </button>
```

d) Just before the closing `</li>` of the `JobCard` return, render the picker:

```tsx
      {showPicker && (
        <SourcePicker
          jobId={job.id}
          onClose={() => setShowPicker(false)}
          onAssigned={onChanged}
        />
      )}
```

- [ ] **Step 5: Add minimal styles**

In `frontend/src/App.css`, append picker styles (match the existing dark theme variables already used in the file):

```css
.picker-backdrop {
  position: fixed; inset: 0; background: rgba(0, 0, 0, 0.5);
  display: flex; align-items: center; justify-content: center; z-index: 50;
}
.picker-panel {
  background: var(--panel, #1d2630); padding: 1.2rem; border-radius: 8px;
  width: min(560px, 92vw); max-height: 80vh; display: flex; flex-direction: column;
}
.picker-list { list-style: none; margin: 0.6rem 0; padding: 0; overflow-y: auto; }
.picker-list li { padding: 0.2rem 0; }
.picker-list label { display: flex; align-items: center; gap: 0.5rem; cursor: pointer; }
.picker-actions { display: flex; gap: 0.6rem; justify-content: flex-end; }
```

- [ ] **Step 6: Type-check + lint**

Run: `cd frontend && npm run build && npm run lint`
Expected: build succeeds, no type errors.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/types/index.ts frontend/src/api/client.ts frontend/src/components/SourcePicker.tsx frontend/src/components/JobsManager.tsx frontend/src/App.css
git commit -m "feat(ui): add source picker to job cards"
```

---

## Task 8: Backend — dashboard endpoint

**Files:**
- Create: `app/schemas/dashboard.py`
- Create: `app/routes/dashboard.py`
- Modify: `app/main.py` (import + `include_router`)
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Produces: `GET /api/dashboard/sources?stale_days=30` → `DashboardResponse` (see schema). Consumed by Task 9.

**Definitions (from spec):**
- `age_seconds` = now − `last_extracted_at`, `null` if never extracted.
- `stale` = extracted but `last_extracted_at` older than `stale_days` (default 30). Never-extracted sources are counted under `never_extracted`, not `stale`.
- `failing` = `source.status == FAILED`.
- `running` = `source.status == EXTRACTING`.
- `article_count` excludes removed articles (`Article.removed_at IS NULL`).
- `last_run_*` from the source's most recent `ExtractionRun` (by `started_at`).

- [ ] **Step 1: Write the failing test**

Create `tests/test_dashboard.py`:

```python
"""GET /api/dashboard/sources returns summary + per-source health rows."""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

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
from app.models import Vendor, Product, DocumentationSource, ExtractionRun, Article
from app.models.source import SourceStatus
from app.models.extraction_run import RunStatus

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def client():
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


async def test_dashboard_summary_and_rows(client):
    c, factory = client
    now = datetime.now(timezone.utc)
    async with factory() as s:
        v = Vendor(name="Acme"); s.add(v); await s.flush()
        p = Product(vendor_id=v.id, name="Cloud"); s.add(p); await s.flush()

        fresh = DocumentationSource(
            product_id=p.id, name="Fresh", base_url="https://d/1",
            status=SourceStatus.COMPLETED, last_extracted_at=now - timedelta(days=1),
        )
        stale = DocumentationSource(
            product_id=p.id, name="Stale", base_url="https://d/2",
            status=SourceStatus.COMPLETED, last_extracted_at=now - timedelta(days=40),
        )
        never = DocumentationSource(
            product_id=p.id, name="Never", base_url="https://d/3",
            status=SourceStatus.PENDING,
        )
        failed = DocumentationSource(
            product_id=p.id, name="Failed", base_url="https://d/4",
            status=SourceStatus.FAILED, last_extracted_at=now - timedelta(days=2),
        )
        s.add_all([fresh, stale, never, failed]); await s.flush()

        run = ExtractionRun(
            source_id=fresh.id, status=RunStatus.COMPLETED,
            started_at=now - timedelta(days=1),
            articles_extracted=3, articles_updated=1, articles_unchanged=5,
        )
        s.add(run); await s.flush()
        # one active + one removed article on fresh
        s.add(Article(source_id=fresh.id, title="A", source_url="https://d/1/a",
                      topic_key="a", content_markdown="x"))
        s.add(Article(source_id=fresh.id, title="B", source_url="https://d/1/b",
                      topic_key="b", content_markdown="x",
                      removed_at=now - timedelta(days=1)))
        await s.commit()

    body = (await c.get("/api/dashboard/sources?stale_days=30")).json()
    summary = body["summary"]
    assert summary["total"] == 4
    assert summary["never_extracted"] == 1
    assert summary["stale"] == 1      # Stale only; Never is not stale
    assert summary["failing"] == 1

    rows = {r["name"]: r for r in body["sources"]}
    assert rows["Never"]["age_seconds"] is None
    assert rows["Fresh"]["article_count"] == 1   # removed article excluded
    assert rows["Fresh"]["last_run_status"] == "completed"
    assert rows["Fresh"]["last_run_new"] == 3
    assert rows["Fresh"]["vendor_name"] == "Acme"
```

> The `Article` NOT-NULL columns without defaults are `source_id, title, source_url, topic_key, content_markdown` (verified against `app/models/article.py`); the constructions above supply all of them. No extra kwargs needed.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_dashboard.py -v`
Expected: FAIL — 404 (route not registered), `body["summary"]` raises `KeyError`.

- [ ] **Step 3: Create the schemas**

Create `app/schemas/dashboard.py`:

```python
"""Dashboard response schemas — per-source extraction health."""
import uuid

from pydantic import BaseModel


class DashboardSummary(BaseModel):
    total: int
    never_extracted: int
    stale: int
    failing: int
    running: int


class DashboardSourceRow(BaseModel):
    id: uuid.UUID
    name: str
    vendor_name: str
    product_name: str
    status: str
    last_extracted_at: str | None
    age_seconds: int | None
    article_count: int
    last_run_status: str | None
    last_run_new: int | None
    last_run_updated: int | None
    last_run_unchanged: int | None
    job_id: uuid.UUID | None
    job_name: str | None
    next_run_at: str | None


class DashboardResponse(BaseModel):
    summary: DashboardSummary
    sources: list[DashboardSourceRow]
```

- [ ] **Step 4: Create the route**

Create `app/routes/dashboard.py`:

```python
"""Dashboard route — per-source extraction health overview."""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.article import Article
from app.models.extraction_run import ExtractionRun
from app.models.job import Job
from app.models.product import Product
from app.models.source import DocumentationSource, SourceStatus
from app.models.vendor import Vendor
from app.schemas.dashboard import (
    DashboardResponse, DashboardSourceRow, DashboardSummary,
)

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/sources", response_model=DashboardResponse)
async def dashboard_sources(
    stale_days: int = Query(30, ge=1),
    db: AsyncSession = Depends(get_db),
):
    now = datetime.now(timezone.utc)

    # Active article counts per source (removed excluded).
    counts: dict = {}
    for sid, n in await db.execute(
        select(Article.source_id, func.count())
        .where(Article.removed_at.is_(None))
        .group_by(Article.source_id)
    ):
        counts[sid] = n

    # Latest run per source by started_at (small data set — fetch newest-first
    # and keep the first seen per source).
    latest_run: dict = {}
    for run in (
        await db.execute(
            select(ExtractionRun).order_by(ExtractionRun.started_at.desc())
        )
    ).scalars():
        latest_run.setdefault(run.source_id, run)

    rows_q = (
        select(
            DocumentationSource,
            Vendor.name.label("vendor_name"),
            Product.name.label("product_name"),
            Job.id.label("job_id"),
            Job.name.label("job_name"),
            Job.next_run_at.label("next_run_at"),
        )
        .join(Product, DocumentationSource.product_id == Product.id)
        .join(Vendor, Product.vendor_id == Vendor.id)
        .outerjoin(Job, DocumentationSource.job_id == Job.id)
        .order_by(Vendor.name, Product.name, DocumentationSource.name)
    )
    rows = (await db.execute(rows_q)).all()

    out: list[DashboardSourceRow] = []
    total = never = stale = failing = running = 0
    stale_cutoff = now - timedelta(days=stale_days)

    for src, vendor_name, product_name, job_id, job_name, next_run_at in rows:
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
        out.append(DashboardSourceRow(
            id=src.id, name=src.name,
            vendor_name=vendor_name, product_name=product_name,
            status=src.status.value,
            last_extracted_at=last.isoformat() if last else None,
            age_seconds=age,
            article_count=counts.get(src.id, 0),
            last_run_status=run.status.value if run else None,
            last_run_new=run.articles_extracted if run else None,
            last_run_updated=run.articles_updated if run else None,
            last_run_unchanged=run.articles_unchanged if run else None,
            job_id=job_id, job_name=job_name,
            next_run_at=next_run_at.isoformat() if next_run_at else None,
        ))

    return DashboardResponse(
        summary=DashboardSummary(
            total=total, never_extracted=never, stale=stale,
            failing=failing, running=running,
        ),
        sources=out,
    )
```

- [ ] **Step 5: Register the router in main.py**

In `app/main.py`, add `dashboard` to the `from app.routes import (...)` block (import as `dashboard_router` matching the existing alias style — check how others are aliased; if they import the module's `router`, mirror that exactly), then add:

```python
app.include_router(dashboard_router)
```

after `app.include_router(profiles_router)`.

- [ ] **Step 6: Run test to verify it passes**

Run: `pytest tests/test_dashboard.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add app/schemas/dashboard.py app/routes/dashboard.py app/main.py tests/test_dashboard.py
git commit -m "feat(dashboard): per-source extraction health endpoint"
```

---

## Task 9: Frontend — Dashboard view

**Files:**
- Modify: `frontend/src/types/index.ts` (add `DashboardSummary`, `DashboardSourceRow`, `DashboardResponse`)
- Modify: `frontend/src/api/client.ts` (add `getDashboard`)
- Create: `frontend/src/components/Dashboard.tsx`
- Modify: `frontend/src/App.tsx` (add `dashboard` view + nav button + select-source flow)
- Test: `cd frontend && npm run build` + `npm run lint`

**Interfaces:**
- Consumes: `GET /api/dashboard/sources` (Task 8), `getSource(id)` (existing client) for row → Browse navigation.

- [ ] **Step 1: Add the types**

In `frontend/src/types/index.ts`:

```typescript
export interface DashboardSummary {
  total: number;
  never_extracted: number;
  stale: number;
  failing: number;
  running: number;
}

export interface DashboardSourceRow {
  id: string;
  name: string;
  vendor_name: string;
  product_name: string;
  status: string;
  last_extracted_at: string | null;
  age_seconds: number | null;
  article_count: number;
  last_run_status: string | null;
  last_run_new: number | null;
  last_run_updated: number | null;
  last_run_unchanged: number | null;
  job_id: string | null;
  job_name: string | null;
  next_run_at: string | null;
}

export interface DashboardResponse {
  summary: DashboardSummary;
  sources: DashboardSourceRow[];
}
```

- [ ] **Step 2: Add the client function**

In `frontend/src/api/client.ts`, add `DashboardResponse` to the type imports and:

```typescript
export async function getDashboard(staleDays = 30): Promise<DashboardResponse> {
  const res = await api.get<DashboardResponse>("/dashboard/sources", {
    params: { stale_days: staleDays },
  });
  return res.data;
}
```

- [ ] **Step 3: Create the Dashboard component**

Create `frontend/src/components/Dashboard.tsx`:

```tsx
import { useCallback, useEffect, useMemo, useState } from "react";
import type { DashboardResponse, DashboardSourceRow } from "../types";
import { getDashboard, getSource } from "../api/client";
import type { DocumentationSource } from "../types";

function fmtAge(seconds: number | null): string {
  if (seconds === null) return "never";
  const d = Math.floor(seconds / 86400);
  if (d >= 1) return `${d}d ago`;
  const h = Math.floor(seconds / 3600);
  if (h >= 1) return `${h}h ago`;
  const m = Math.floor(seconds / 60);
  return `${m}m ago`;
}

// Surface problems first: never → failed → stale → rest, then by name.
function healthRank(r: DashboardSourceRow, staleSeconds: number): number {
  if (r.age_seconds === null) return 0;
  if (r.status === "failed") return 1;
  if (r.age_seconds > staleSeconds) return 2;
  return 3;
}

export default function Dashboard({
  onSelectSource,
}: {
  onSelectSource: (s: DocumentationSource) => void;
}) {
  const [data, setData] = useState<DashboardResponse | null>(null);
  const [error, setError] = useState("");
  const staleSeconds = 30 * 86400;

  const refresh = useCallback(async () => {
    try {
      setData(await getDashboard(30));
    } catch {
      setError("Failed to load dashboard");
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const sorted = useMemo(() => {
    if (!data) return [];
    return [...data.sources].sort((a, b) => {
      const ra = healthRank(a, staleSeconds);
      const rb = healthRank(b, staleSeconds);
      if (ra !== rb) return ra - rb;
      return `${a.vendor_name}${a.product_name}${a.name}`.localeCompare(
        `${b.vendor_name}${b.product_name}${b.name}`,
      );
    });
  }, [data, staleSeconds]);

  const openSource = async (id: string) => {
    try {
      onSelectSource(await getSource(id));
    } catch {
      setError("Failed to open source");
    }
  };

  if (error) return <div className="error">{error}</div>;
  if (!data) return <p className="sub">Loading…</p>;

  const s = data.summary;
  return (
    <div className="dashboard">
      <h2>Dashboard</h2>
      <div className="tile-row">
        <div className="tile"><span className="tile-n">{s.total}</span>Sources</div>
        <div className="tile warn"><span className="tile-n">{s.never_extracted}</span>Never extracted</div>
        <div className="tile warn"><span className="tile-n">{s.stale}</span>Stale (&gt;30d)</div>
        <div className="tile bad"><span className="tile-n">{s.failing}</span>Failing</div>
        <div className="tile"><span className="tile-n">{s.running}</span>Running</div>
      </div>
      <table className="dashboard-table">
        <thead>
          <tr>
            <th>Source</th><th>Status</th><th>Last extracted</th>
            <th>Articles</th><th>Last run</th><th>Job</th>
          </tr>
        </thead>
        <tbody>
          {sorted.map((r) => (
            <tr key={r.id} onClick={() => openSource(r.id)} className="clickable-row">
              <td>{[r.vendor_name, r.product_name, r.name].join(" › ")}</td>
              <td>{r.status}</td>
              <td>{fmtAge(r.age_seconds)}</td>
              <td>{r.article_count}</td>
              <td>
                {r.last_run_status
                  ? `${r.last_run_status} (${r.last_run_new ?? 0}n/${r.last_run_updated ?? 0}u/${r.last_run_unchanged ?? 0}=)`
                  : "—"}
              </td>
              <td>{r.job_name ?? "—"}</td>
            </tr>
          ))}
          {sorted.length === 0 && (
            <tr><td colSpan={6} className="sub">No sources yet.</td></tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
```

- [ ] **Step 4: Add the Dashboard view to App.tsx**

In `frontend/src/App.tsx`:

a) Add the import:

```typescript
import Dashboard from "./components/Dashboard";
```

b) Add `"dashboard"` to the `View` union type.

c) Add a nav button next to the Jobs button (after it, before `</nav>`):

```tsx
          <button
            className={view === "dashboard" ? "active" : ""}
            onClick={() => setView("dashboard")}
          >
            Dashboard
          </button>
```

d) In `app-main`, render it (next to the jobs view block):

```tsx
        {view === "dashboard" && (
          <Dashboard onSelectSource={handleSelectSource} />
        )}
```

`handleSelectSource` already sets the selected source and switches to `browse`.

- [ ] **Step 5: Add styles**

In `frontend/src/App.css`, append:

```css
.tile-row { display: flex; gap: 0.8rem; flex-wrap: wrap; margin: 1rem 0; }
.tile {
  background: var(--panel, #1d2630); padding: 0.8rem 1.1rem; border-radius: 8px;
  display: flex; flex-direction: column; min-width: 110px;
}
.tile-n { font-size: 1.6rem; font-weight: 700; }
.tile.warn .tile-n { color: var(--amber, #eaa53d); }
.tile.bad .tile-n { color: #e0685f; }
.dashboard-table { width: 100%; border-collapse: collapse; }
.dashboard-table th, .dashboard-table td {
  text-align: left; padding: 0.5rem 0.6rem; border-bottom: 1px solid rgba(255,255,255,0.08);
}
.clickable-row { cursor: pointer; }
.clickable-row:hover { background: rgba(255,255,255,0.04); }
```

- [ ] **Step 6: Type-check + lint**

Run: `cd frontend && npm run build && npm run lint`
Expected: build succeeds, no type errors.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/types/index.ts frontend/src/api/client.ts frontend/src/components/Dashboard.tsx frontend/src/App.tsx frontend/src/App.css
git commit -m "feat(ui): add source health dashboard"
```

---

## Task 10: Backend — CSV bulk import of sources

**Files:**
- Modify: `app/schemas/source.py` (add `SourceImportRequest`, `SourceImportRow`, `SourceImportResult`)
- Modify: `app/routes/sources.py` (add `POST /import` route)
- Test: `tests/test_source_import.py`

**Interfaces:**
- Produces: `POST /api/sources/import` body `{ "csv": "<text>" }` → `SourceImportResult`. Consumed by Task 11.

**Note:** Declare `POST /import` — its path does not collide with the existing `POST ""` create route. Matching: vendors by case-insensitive trimmed name; products by (vendor, case-insensitive trimmed name); skip when a source with the same `(product_id, base_url)` already exists.

- [ ] **Step 1: Write the failing test**

Create `tests/test_source_import.py`:

```python
"""POST /api/sources/import — CSV bulk import with auto-created vendors/products."""
import os
import sys

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
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
async def client():
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


CSV = (
    "vendor,product,source_name,base_url,url_template\n"
    "Acme,Cloud,Guide,https://acme/guide,\n"
    "Acme,Cloud,API,https://acme/api,https://acme/api/{version}\n"
    "Beta,Box,Docs,https://beta/docs,\n"
)


async def test_import_creates_vendors_products_sources(client):
    c, factory = client
    res = (await c.post("/api/sources/import", json={"csv": CSV})).json()
    assert res["created"] == 3 and res["skipped"] == 0 and res["errors"] == 0

    async with factory() as s:
        assert (await s.execute(select(func.count()).select_from(Vendor))).scalar() == 2
        assert (await s.execute(select(func.count()).select_from(Product))).scalar() == 2
        assert (await s.execute(select(func.count()).select_from(DocumentationSource))).scalar() == 3


async def test_import_reuses_existing_and_skips_duplicate(client):
    c, factory = client
    await c.post("/api/sources/import", json={"csv": CSV})
    # Re-import the same CSV: same (product, base_url) → all skipped, no new vendors.
    res = (await c.post("/api/sources/import", json={"csv": CSV})).json()
    assert res["created"] == 0 and res["skipped"] == 3

    async with factory() as s:
        assert (await s.execute(select(func.count()).select_from(Vendor))).scalar() == 2


async def test_import_bad_row_recorded_without_aborting(client):
    c, _ = client
    csv = (
        "vendor,product,source_name,base_url\n"
        "Acme,Cloud,Good,https://acme/good\n"
        "Acme,Cloud,,https://acme/missing-name\n"   # missing source_name
    )
    res = (await c.post("/api/sources/import", json={"csv": csv})).json()
    assert res["created"] == 1 and res["errors"] == 1
    bad = next(r for r in res["rows"] if r["result"] == "error")
    assert "source_name" in bad["message"]


async def test_import_malformed_csv_is_422(client):
    c, _ = client
    res = await c.post("/api/sources/import", json={"csv": "not a real,csv\nonly one row"})
    assert res.status_code == 422
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_source_import.py -v`
Expected: FAIL — 404/405 (route missing) so `.json()["created"]` raises `KeyError`.

- [ ] **Step 3: Add the schemas**

In `app/schemas/source.py`:

```python
class SourceImportRequest(BaseModel):
    csv: str


class SourceImportRow(BaseModel):
    row: int
    result: str  # "created" | "skipped" | "error"
    vendor: str | None = None
    product: str | None = None
    source_name: str | None = None
    message: str = ""


class SourceImportResult(BaseModel):
    created: int
    skipped: int
    errors: int
    rows: list[SourceImportRow]
```

- [ ] **Step 4: Add the import route**

In `app/routes/sources.py`, add `import csv as csvlib` and `import io` at the top, add the new schemas to the `from app.schemas.source import (...)` block, and add this route (above `@router.get("/{source_id}")`, alongside `/pickable`):

```python
REQUIRED_COLUMNS = {"vendor", "product", "source_name", "base_url"}


@router.post("/import", response_model=SourceImportResult)
async def import_sources(body: SourceImportRequest, db: AsyncSession = Depends(get_db)):
    """Bulk-import sources from CSV. Auto-creates vendors/products by name;
    skips a source when (product, base_url) already exists."""
    reader = csvlib.DictReader(io.StringIO(body.csv))
    if reader.fieldnames is None or not REQUIRED_COLUMNS.issubset(
        {(f or "").strip().lower() for f in reader.fieldnames}
    ):
        raise HTTPException(
            status_code=422,
            detail=f"CSV must have columns: {', '.join(sorted(REQUIRED_COLUMNS))}",
        )

    # In-request caches keyed by lowercased trimmed names.
    vendor_cache: dict[str, Vendor] = {}
    product_cache: dict[tuple[str, str], Product] = {}
    rows: list[SourceImportRow] = []
    created = skipped = errors = 0

    async def _vendor(name: str) -> Vendor:
        key = name.lower()
        if key in vendor_cache:
            return vendor_cache[key]
        existing = (
            await db.execute(
                select(Vendor).where(func.lower(Vendor.name) == key)
            )
        ).scalar_one_or_none()
        if existing is None:
            existing = Vendor(name=name)
            db.add(existing)
            await db.flush()
        vendor_cache[key] = existing
        return existing

    async def _product(vendor: Vendor, name: str) -> Product:
        key = (str(vendor.id), name.lower())
        if key in product_cache:
            return product_cache[key]
        existing = (
            await db.execute(
                select(Product).where(
                    Product.vendor_id == vendor.id,
                    func.lower(Product.name) == name.lower(),
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            existing = Product(vendor_id=vendor.id, name=name)
            db.add(existing)
            await db.flush()
        product_cache[key] = existing
        return existing

    # Row numbers start at 2 (row 1 is the header).
    for i, raw in enumerate(reader, start=2):
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        vendor_name = row.get("vendor", "")
        product_name = row.get("product", "")
        source_name = row.get("source_name", "")
        base_url = row.get("base_url", "")
        url_template = row.get("url_template", "") or None

        missing = [
            c for c, val in (
                ("vendor", vendor_name), ("product", product_name),
                ("source_name", source_name), ("base_url", base_url),
            ) if not val
        ]
        if missing:
            errors += 1
            rows.append(SourceImportRow(
                row=i, result="error", vendor=vendor_name or None,
                product=product_name or None, source_name=source_name or None,
                message=f"missing required value(s): {', '.join(missing)}",
            ))
            continue

        vendor = await _vendor(vendor_name)
        product = await _product(vendor, product_name)

        dup = (
            await db.execute(
                select(DocumentationSource.id).where(
                    DocumentationSource.product_id == product.id,
                    DocumentationSource.base_url == base_url,
                )
            )
        ).scalar_one_or_none()
        if dup is not None:
            skipped += 1
            rows.append(SourceImportRow(
                row=i, result="skipped", vendor=vendor_name,
                product=product_name, source_name=source_name,
                message="source with this base_url already exists",
            ))
            continue

        db.add(DocumentationSource(
            product_id=product.id, name=source_name,
            base_url=base_url, url_template=url_template,
        ))
        created += 1
        rows.append(SourceImportRow(
            row=i, result="created", vendor=vendor_name,
            product=product_name, source_name=source_name,
        ))

    await db.commit()
    return SourceImportResult(
        created=created, skipped=skipped, errors=errors, rows=rows,
    )
```

> The `test_import_malformed_csv_is_422` input has a header `not a real,csv` lacking the required columns, so it triggers the 422 guard. Confirm the required-columns check rejects it.

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_source_import.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/schemas/source.py app/routes/sources.py tests/test_source_import.py
git commit -m "feat(sources): CSV bulk import with auto-created vendors/products"
```

---

## Task 11: Frontend — BulkImport panel on the dashboard

**Files:**
- Modify: `frontend/src/types/index.ts` (add `SourceImportRow`, `SourceImportResult`)
- Modify: `frontend/src/api/client.ts` (add `importSources`)
- Create: `frontend/src/components/BulkImport.tsx`
- Modify: `frontend/src/components/Dashboard.tsx` (add "Import CSV" button + panel + refresh)
- Test: `cd frontend && npm run build` + `npm run lint`

**Interfaces:**
- Consumes: `POST /api/sources/import` (Task 10). On success calls the dashboard's `refresh`.

- [ ] **Step 1: Add the types**

In `frontend/src/types/index.ts`:

```typescript
export interface SourceImportRow {
  row: number;
  result: string;
  vendor: string | null;
  product: string | null;
  source_name: string | null;
  message: string;
}

export interface SourceImportResult {
  created: number;
  skipped: number;
  errors: number;
  rows: SourceImportRow[];
}
```

- [ ] **Step 2: Add the client function**

In `frontend/src/api/client.ts`, add `SourceImportResult` to the type imports and:

```typescript
export async function importSources(csvText: string): Promise<SourceImportResult> {
  const res = await api.post<SourceImportResult>("/sources/import", { csv: csvText });
  return res.data;
}
```

- [ ] **Step 3: Create the BulkImport component**

Create `frontend/src/components/BulkImport.tsx`:

```tsx
import { useState } from "react";
import type { SourceImportResult } from "../types";
import { importSources } from "../api/client";

const SAMPLE = "vendor,product,source_name,base_url,url_template";

export default function BulkImport({
  onClose,
  onImported,
}: {
  onClose: () => void;
  onImported: () => void;
}) {
  const [csv, setCsv] = useState("");
  const [result, setResult] = useState<SourceImportResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const onFile = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    file.text().then(setCsv);
  };

  const submit = async () => {
    setBusy(true);
    setError("");
    try {
      const r = await importSources(csv);
      setResult(r);
      onImported();
    } catch (err: any) {
      setError(err.response?.data?.detail || "Import failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="picker-backdrop" onClick={onClose}>
      <div className="picker-panel" onClick={(e) => e.stopPropagation()}>
        <h3>Import sources (CSV)</h3>
        <p className="sub">Columns: {SAMPLE}</p>
        {error && <div className="error">{error}</div>}
        <input type="file" accept=".csv,text/csv" onChange={onFile} />
        <textarea
          rows={8}
          placeholder={SAMPLE}
          value={csv}
          onChange={(e) => setCsv(e.target.value)}
          style={{ width: "100%", marginTop: "0.5rem" }}
        />
        {result && (
          <div className="import-result">
            <p className="sub">
              Created {result.created} · Skipped {result.skipped} · Errors {result.errors}
            </p>
            <ul className="picker-list">
              {result.rows
                .filter((r) => r.result !== "created")
                .map((r) => (
                  <li key={r.row} className="sub">
                    Row {r.row}: {r.result} — {r.message}
                  </li>
                ))}
            </ul>
          </div>
        )}
        <div className="picker-actions">
          <button className="btn-secondary-sm" onClick={onClose}>Close</button>
          <button
            className="btn-primary-sm"
            disabled={busy || !csv.trim()}
            onClick={submit}
          >
            {busy ? "Importing…" : "Import"}
          </button>
        </div>
      </div>
    </div>
  );
}
```

- [ ] **Step 4: Wire BulkImport into the Dashboard**

In `frontend/src/components/Dashboard.tsx`:

a) Add the import:

```typescript
import BulkImport from "./BulkImport";
```

b) Add state near the other hooks:

```typescript
  const [showImport, setShowImport] = useState(false);
```

c) Replace the `<h2>Dashboard</h2>` line with a header row containing the button:

```tsx
      <div className="dashboard-header">
        <h2>Dashboard</h2>
        <button className="btn-primary-sm" onClick={() => setShowImport(true)}>
          Import CSV
        </button>
      </div>
```

d) Before the closing `</div>` of the component's returned `dashboard` div, render the modal:

```tsx
      {showImport && (
        <BulkImport
          onClose={() => setShowImport(false)}
          onImported={refresh}
        />
      )}
```

- [ ] **Step 5: Add header style**

In `frontend/src/App.css`, append:

```css
.dashboard-header {
  display: flex; align-items: center; justify-content: space-between; gap: 1rem;
}
.import-result { margin-top: 0.6rem; max-height: 30vh; overflow-y: auto; }
```

- [ ] **Step 6: Type-check + lint**

Run: `cd frontend && npm run build && npm run lint`
Expected: build succeeds, no type errors.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/types/index.ts frontend/src/api/client.ts frontend/src/components/BulkImport.tsx frontend/src/components/Dashboard.tsx frontend/src/App.css
git commit -m "feat(ui): CSV bulk import panel on dashboard"
```

---

## Final verification

- [ ] **Run the full backend test suite**

Run: `pytest -q`
Expected: all tests pass (port-forward the homelab Postgres for the test DB if running locally — see project memory).

- [ ] **Build the frontend**

Run: `cd frontend && npm run build && npm run lint`
Expected: clean build, no lint errors.

- [ ] **Apply the migration against a real DB**

Run: `alembic upgrade head`
Expected: `articles_resumed` column added; no errors.
