# Delta-Feed Deletion Tombstones + Resumable Bootstrap — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deleting a source/product/vendor emits `removed` delta-feed tombstones (with the article id preserved through the hard delete), and the bootstrap snapshot becomes resumable without missing updates.

**Architecture:** Make `content_changes` a true append-only log by dropping its three `ON DELETE SET NULL` FKs, then have the deletion routes tombstone live articles before the cascade purge. Separately, add a `bootstrap_after` keyset param plus an up-front `bootstrap_start` watermark line so a dropped bootstrap resumes against the original watermark.

**Tech Stack:** FastAPI, async SQLAlchemy (asyncpg), PostgreSQL, Alembic, Pydantic v2, pytest + httpx AsyncClient.

**Spec:** `docs/superpowers/specs/2026-07-11-delta-feed-deletion-and-bootstrap-resume-design.md`

## Global Constraints

- **`content_changes` is append-only.** No path may mutate a historical row. Out-of-band deletions write `removed` rows in the *same transaction* as the entity delete, with `run_id=None`. **Never** write a `run_start` sentinel for an out-of-band deletion (there is no run to protect).
- **`_tombstone_record` is already self-contained** (`delta_feed.py:144` — `removed_at`←`change.created_at`, ids from the change row). Do NOT change it. It must keep working with a dangling/hard-deleted article.
- **Tests are async** for this feature: mirror the `ctx` fixture in `tests/test_delta_feed.py` (asyncpg engine, `Base.metadata.drop_all`/`create_all`, `app.dependency_overrides[get_db]`, `AsyncClient(ASGITransport(app))`). `TEST_DATABASE_URL = settings.database_url.rsplit("/",1)[0] + "/docextractor_test"`. Auth is disabled in tests (no `DOCEXTRACTOR_AUTH_JWT_SECRET`), so API calls are unauthenticated. Run: `cd backend && ./venv/bin/pytest tests/<file> -v`.
- **Backward compatibility:** the new `bootstrap_start` control line uses a distinct `control` type; the terminal `{"control":"cursor",...}` line and all record shapes are unchanged.
- Frequent commits: one commit per task (after its tests pass). DRY, YAGNI, TDD.

## File Structure

- `backend/alembic/versions/<new>.py` — **new** migration: drop three FKs; downgrade re-adds them.
- `backend/app/models/content_change.py` — drop `ForeignKey(...)` from `article_id`, `source_id`, `run_id` (plain nullable UUID).
- `backend/app/services/change_log.py` — widen `record_removals` `run_id` to `Optional`; add `record_source_deletions`.
- `backend/app/routes/sources.py`, `products.py`, `vendors.py` — tombstone before delete.
- `backend/app/services/delta_feed.py` — `bootstrap_after` filter + `bootstrap_start` line.
- `backend/app/routes/articles.py` — `bootstrap_after` query param.
- `docs/API.md` — deletion + resume semantics.
- `backend/tests/test_deletion_tombstones.py` (**new**), `backend/tests/test_bootstrap_resume.py` (**new**), `backend/tests/test_change_log.py` (extend).

---

### Task 1: Append-only outbox — drop the `content_changes` FKs

**Files:**
- Modify: `backend/app/models/content_change.py`
- Create: `backend/alembic/versions/<new>_content_changes_append_only.py`
- Test: `backend/tests/test_deletion_tombstones.py` (new — first test)

**Interfaces:**
- Produces: `content_changes.article_id/source_id/run_id` are plain nullable `UUID` columns (no FK) — a hard-deleted article/source/run no longer nulls or removes the outbox row.

- [ ] **Step 1: Write the failing test** — `backend/tests/test_deletion_tombstones.py`

```python
"""Deletion tombstones + append-only content_changes (async httpx client)."""
import os
import sys
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core.database import Base, get_db
from app.main import app
from app.models import Vendor, Product, DocumentationSource, Article
from app.models.content_change import ContentChange, ChangeType

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


async def _seed_article(factory):
    async with factory() as s:
        v = Vendor(name="V"); s.add(v); await s.flush()
        p = Product(name="P", vendor_id=v.id); s.add(p); await s.flush()
        src = DocumentationSource(name="S", base_url="https://s", product_id=p.id, source_type="web")
        s.add(src); await s.flush()
        art = Article(source_id=src.id, title="A", source_url="https://s/a",
                      topic_key="https://s/a", content_markdown="# A", content_hash="h")
        s.add(art); await s.flush()
        cc = ContentChange(article_id=art.id, source_id=src.id, run_id=None,
                           change_type=ChangeType.ADDED.value, content_hash="h",
                           topic_key="https://s/a")
        s.add(cc); await s.commit()
        return src.id, art.id, cc.id


async def test_hard_deleting_article_preserves_outbox_ids(ctx):
    """content_changes is append-only: deleting the article must NOT null its
    article_id on the historical outbox row."""
    _c, factory = ctx
    src_id, art_id, cc_id = await _seed_article(factory)
    async with factory() as s:
        art = (await s.execute(select(Article).where(Article.id == art_id))).scalar_one()
        await s.delete(art)
        await s.commit()
    async with factory() as s:
        cc = (await s.execute(select(ContentChange).where(ContentChange.id == cc_id))).scalar_one()
        assert cc.article_id == art_id      # was nulled by the SET NULL FK before this task
        assert cc.source_id == src_id
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && ./venv/bin/pytest tests/test_deletion_tombstones.py::test_hard_deleting_article_preserves_outbox_ids -v`
Expected: FAIL — `assert cc.article_id == art_id` fails because the `ON DELETE SET NULL` FK sets `article_id` to `None`.

- [ ] **Step 3: Drop the FKs in the model** — `backend/app/models/content_change.py`

Change the three columns from FK to plain nullable UUID:

```python
    article_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
```

Remove the now-unused `ForeignKey` import if nothing else in the file uses it (the file still imports `BigInteger, DateTime, Index, String, func` — drop only `ForeignKey`). Update the module docstring's line about `article_id` FK to say it is an unconstrained historical reference.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && ./venv/bin/pytest tests/test_deletion_tombstones.py::test_hard_deleting_article_preserves_outbox_ids -v`
Expected: PASS.

- [ ] **Step 5: Write the Alembic migration** — `backend/alembic/versions/<new>_content_changes_append_only.py`

Generate the revision id with `cd backend && alembic revision -m "content_changes append-only (drop SET NULL FKs)"` then fill:

```python
def upgrade() -> None:
    op.drop_constraint("content_changes_article_id_fkey", "content_changes", type_="foreignkey")
    op.drop_constraint("content_changes_source_id_fkey", "content_changes", type_="foreignkey")
    op.drop_constraint("content_changes_run_id_fkey", "content_changes", type_="foreignkey")


def downgrade() -> None:
    op.create_foreign_key("content_changes_run_id_fkey", "content_changes",
                          "extraction_runs", ["run_id"], ["id"], ondelete="SET NULL")
    op.create_foreign_key("content_changes_source_id_fkey", "content_changes",
                          "documentation_sources", ["source_id"], ["id"], ondelete="SET NULL")
    op.create_foreign_key("content_changes_article_id_fkey", "content_changes",
                          "articles", ["article_id"], ["id"], ondelete="SET NULL")
```

Set `down_revision` to the current head (`cd backend && alembic heads` to confirm). The default PostgreSQL constraint names are `content_changes_<column>_fkey`; if `alembic heads`/inspection shows different names, use the actual ones.

- [ ] **Step 6: Verify the migration applies against a scratch DB**

Run: `cd backend && alembic upgrade head` (against the dev DB), then `alembic downgrade -1`, then `alembic upgrade head` again. Expected: no errors; head matches the new revision.

- [ ] **Step 7: Commit**

```bash
git add backend/app/models/content_change.py backend/alembic/versions/ backend/tests/test_deletion_tombstones.py
git commit -m "feat(outbox): make content_changes append-only (drop SET NULL FKs)"
```

---

### Task 2: `record_source_deletions` helper

**Files:**
- Modify: `backend/app/services/change_log.py`
- Test: `backend/tests/test_change_log.py` (extend)

**Interfaces:**
- Consumes: `ContentChange`, `Article` (existing).
- Produces:
  - `record_removals(db, *, rows, source_id, run_id: uuid.UUID | None)` — `run_id` now optional.
  - `async def record_source_deletions(db, *, source_ids) -> int` — for every **live** (`removed_at IS NULL`) article in `source_ids`, `db.add` one `removed` `ContentChange` (`run_id=None`, real `article_id`/`source_id`/`topic_key`); returns the count. Caller commits. Must be called **before** the entities are deleted.

- [ ] **Step 1: Write the failing test** — append to `backend/tests/test_change_log.py`

Match the file's existing fixture/session style. If the file uses an async factory named `factory`/`ctx`, reuse it; otherwise add a local async fixture identical to Task 1's `ctx` (yielding `factory`). Test:

```python
async def test_record_source_deletions_tombstones_live_articles_only(factory):
    from app.services import change_log
    from app.models.content_change import ContentChange, ChangeType
    from sqlalchemy import select
    from datetime import datetime, timezone

    async with factory() as s:
        v = Vendor(name="V"); s.add(v); await s.flush()
        p = Product(name="P", vendor_id=v.id); s.add(p); await s.flush()
        src = DocumentationSource(name="S", base_url="https://s", product_id=p.id, source_type="web")
        s.add(src); await s.flush()
        live1 = Article(source_id=src.id, title="1", source_url="https://s/1",
                        topic_key="https://s/1", content_markdown="#1", content_hash="h1")
        live2 = Article(source_id=src.id, title="2", source_url="https://s/2",
                        topic_key="https://s/2", content_markdown="#2", content_hash="h2")
        gone = Article(source_id=src.id, title="3", source_url="https://s/3",
                       topic_key="https://s/3", content_markdown="#3", content_hash="h3",
                       removed_at=datetime.now(timezone.utc))
        s.add_all([live1, live2, gone]); await s.commit()
        sid, id1, id2 = src.id, live1.id, live2.id

    async with factory() as s:
        n = await change_log.record_source_deletions(s, source_ids=[sid])
        await s.commit()
        assert n == 2

    async with factory() as s:
        rows = (await s.execute(
            select(ContentChange).where(ContentChange.change_type == ChangeType.REMOVED.value)
        )).scalars().all()
        assert {r.article_id for r in rows} == {id1, id2}      # the removed one excluded
        assert all(r.run_id is None and r.source_id == sid for r in rows)
        assert all(r.topic_key for r in rows)
```

(If `test_change_log.py` has no reusable async `factory` fixture, add one mirroring Task 1's `ctx` but yielding only `factory`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && ./venv/bin/pytest tests/test_change_log.py::test_record_source_deletions_tombstones_live_articles_only -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'record_source_deletions'`.

- [ ] **Step 3: Implement** — `backend/app/services/change_log.py`

Widen `record_removals` signature to `run_id: uuid.UUID | None` (body unchanged). Add:

```python
from collections.abc import Sequence


async def record_source_deletions(
    db: AsyncSession, *, source_ids: Sequence[uuid.UUID]
) -> int:
    """Tombstone every live article in *source_ids* (run_id NULL); caller commits.

    Must run BEFORE the sources/articles are deleted, while the articles still
    exist. Returns the number of removed rows written."""
    if not source_ids:
        return 0
    rows = (await db.execute(
        select(Article.id, Article.source_id, Article.topic_key).where(
            Article.source_id.in_(source_ids), Article.removed_at.is_(None)
        )
    )).all()
    for art_id, src_id, topic_key in rows:
        db.add(
            ContentChange(
                article_id=art_id,
                source_id=src_id,
                run_id=None,
                change_type=ChangeType.REMOVED.value,
                content_hash=None,
                topic_key=topic_key,
            )
        )
    return len(rows)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && ./venv/bin/pytest tests/test_change_log.py -v`
Expected: PASS (new test + existing tests still green).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/change_log.py backend/tests/test_change_log.py
git commit -m "feat(outbox): record_source_deletions helper + optional run_id"
```

---

### Task 3: Deletion routes tombstone before delete

**Files:**
- Modify: `backend/app/routes/sources.py` (`delete_source`), `backend/app/routes/products.py` (`delete_product`), `backend/app/routes/vendors.py` (`delete_vendor`)
- Modify: `docs/API.md` (deletion note)
- Test: `backend/tests/test_deletion_tombstones.py` (extend)

**Interfaces:**
- Consumes: `change_log.record_source_deletions` (Task 2), append-only outbox (Task 1).
- Produces: `DELETE /api/{sources,products,vendors}/{id}` emit `removed` outbox rows (real ids) for every live descendant article, in the delete transaction.

- [ ] **Step 1: Write the failing tests** — append to `backend/tests/test_deletion_tombstones.py`

```python
from app.models import ExtractionRun
from app.models.extraction_run import RunStatus


async def _seed_two_articles(factory, status=None):
    async with factory() as s:
        v = Vendor(name="V"); s.add(v); await s.flush()
        p = Product(name="P", vendor_id=v.id); s.add(p); await s.flush()
        src = DocumentationSource(name="S", base_url="https://s", product_id=p.id, source_type="web")
        s.add(src); await s.flush()
        a1 = Article(source_id=src.id, title="1", source_url="https://s/1",
                     topic_key="https://s/1", content_markdown="#1", content_hash="h1")
        a2 = Article(source_id=src.id, title="2", source_url="https://s/2",
                     topic_key="https://s/2", content_markdown="#2", content_hash="h2")
        s.add_all([a1, a2]); await s.commit()
        return v.id, p.id, src.id, {a1.id, a2.id}


async def test_delete_source_emits_tombstones_with_intact_ids(ctx):
    c, factory = ctx
    _v, _p, src_id, art_ids = await _seed_two_articles(factory)
    r = await c.delete(f"/api/sources/{src_id}")
    assert r.status_code == 204
    async with factory() as s:
        rows = (await s.execute(
            select(ContentChange).where(ContentChange.change_type == ChangeType.REMOVED.value)
        )).scalars().all()
        assert {row.article_id for row in rows} == art_ids       # ids survived the hard delete
        assert all(row.run_id is None for row in rows)
        # articles are actually gone
        arts = (await s.execute(select(Article).where(Article.source_id == src_id))).scalars().all()
        assert arts == []


async def test_delete_product_and_vendor_emit_tombstones(ctx):
    c, factory = ctx
    _v, p_id, _src, art_ids = await _seed_two_articles(factory)
    assert (await c.delete(f"/api/products/{p_id}")).status_code == 204
    async with factory() as s:
        rows = (await s.execute(select(ContentChange).where(
            ContentChange.change_type == ChangeType.REMOVED.value))).scalars().all()
        assert {row.article_id for row in rows} == art_ids

    v_id, _p, _src, art_ids2 = await _seed_two_articles(factory)
    assert (await c.delete(f"/api/vendors/{v_id}")).status_code == 204
    async with factory() as s:
        rows = (await s.execute(select(ContentChange).where(
            ContentChange.change_type == ChangeType.REMOVED.value))).scalars().all()
        assert art_ids2.issubset({row.article_id for row in rows})


async def test_deletion_removals_respect_safe_ceiling(ctx):
    """A run_id=NULL removal committed above an active run's run_start floor is
    withheld until that run finishes, then served."""
    from app.services import change_log
    c, factory = ctx
    # An unrelated active run with a run_start sentinel (a low floor).
    async with factory() as s:
        v = Vendor(name="RV"); s.add(v); await s.flush()
        p = Product(name="RP", vendor_id=v.id); s.add(p); await s.flush()
        run_src = DocumentationSource(name="RS", base_url="https://r", product_id=p.id, source_type="web")
        s.add(run_src); await s.flush()
        run = ExtractionRun(source_id=run_src.id, status=RunStatus.RUNNING, kind="extract")
        s.add(run); await s.flush()
        await change_log.record_run_start(s, source_id=run_src.id, run_id=run.id)
        await s.commit()
        run_id = run.id

    _v, _p, src_id, _ids = await _seed_two_articles(factory)
    assert (await c.delete(f"/api/sources/{src_id}")).status_code == 204

    # Bootstrap gives us the current cursor; the removals sit above the active
    # run's floor, so an incremental pull withholds them.
    boot = (await c.get("/api/articles/delta")).text.strip().splitlines()
    import json
    cursor = json.loads(boot[-1])["next_since"]
    removed = [json.loads(l) for l in (await c.get(f"/api/articles/delta?since={cursor}")).text.splitlines()
               if l and json.loads(l).get("change_type") == "removed"]
    assert removed == []                       # withheld while the run is active

    async with factory() as s:
        run = (await s.execute(select(ExtractionRun).where(ExtractionRun.id == run_id))).scalar_one()
        run.status = RunStatus.COMPLETED
        await s.commit()

    removed_after = [json.loads(l) for l in (await c.get(f"/api/articles/delta?since={cursor}")).text.splitlines()
                     if l and json.loads(l).get("change_type") == "removed"]
    assert len(removed_after) >= 2             # now served
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && ./venv/bin/pytest tests/test_deletion_tombstones.py -v`
Expected: the three new tests FAIL (no tombstones written; `removed` sets empty).

- [ ] **Step 3: Implement — `delete_source`** (`backend/app/routes/sources.py`)

Add `from app.services import change_log` if not already imported. Before `await db.delete(source)`:

```python
    await change_log.record_source_deletions(db, source_ids=[source_id])
    await db.delete(source)
    await db.commit()
```

- [ ] **Step 4: Implement — `delete_product`** (`backend/app/routes/products.py`)

```python
    source_ids = (await db.execute(
        select(DocumentationSource.id).where(DocumentationSource.product_id == product_id)
    )).scalars().all()
    await change_log.record_source_deletions(db, source_ids=source_ids)
    await db.delete(product)
    await db.commit()
```

Add imports as needed (`from app.services import change_log`, `from app.models import DocumentationSource`, `select`).

- [ ] **Step 5: Implement — `delete_vendor`** (`backend/app/routes/vendors.py`)

```python
    source_ids = (await db.execute(
        select(DocumentationSource.id)
        .join(Product, DocumentationSource.product_id == Product.id)
        .where(Product.vendor_id == vendor_id)
    )).scalars().all()
    await change_log.record_source_deletions(db, source_ids=source_ids)
    await db.delete(vendor)
    await db.commit()
```

Add imports as needed (`change_log`, `DocumentationSource`, `Product`, `select`).

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd backend && ./venv/bin/pytest tests/test_deletion_tombstones.py -v`
Expected: all PASS.

- [ ] **Step 7: Document** — `docs/API.md` (Delta-Feed section)

Add a sentence under the Delta-Feed section: deleting a source/product/vendor now emits `removed` tombstones (with the original `id`), so a consumer that processes `removed` records drops those nodes rather than orphaning them.

- [ ] **Step 8: Commit**

```bash
git add backend/app/routes/sources.py backend/app/routes/products.py backend/app/routes/vendors.py docs/API.md backend/tests/test_deletion_tombstones.py
git commit -m "feat(delta): emit removed tombstones on source/product/vendor delete"
```

---

### Task 4: Resumable bootstrap (`bootstrap_after` + `bootstrap_start` line)

**Files:**
- Modify: `backend/app/services/delta_feed.py` (`stream_bootstrap`)
- Modify: `backend/app/routes/articles.py` (`article_delta_feed`)
- Modify: `docs/API.md` (resume note)
- Test: `backend/tests/test_bootstrap_resume.py` (new)

**Interfaces:**
- Consumes: existing `stream_bootstrap`, `encode_delta_cursor`.
- Produces:
  - `stream_bootstrap(db, *, source_id, vendor_id, visible_vendor_ids, bootstrap_after: uuid.UUID | None = None)` — filters `Article.id > bootstrap_after`; yields a first line `{"control":"bootstrap_start","next_since":<X>}` where `X` is the same watermark as the terminal cursor line.
  - `GET /api/articles/delta?bootstrap_after=<uuid>` — plumbed through (ignored when `since` is present).

- [ ] **Step 1: Write the failing tests** — `backend/tests/test_bootstrap_resume.py`

```python
"""Resumable bootstrap: bootstrap_after + bootstrap_start watermark."""
import json
import os
import sys

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core.database import Base, get_db
from app.main import app
from app.models import Vendor, Product, DocumentationSource, Article

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


async def _seed_n(factory, n):
    async with factory() as s:
        v = Vendor(name="V"); s.add(v); await s.flush()
        p = Product(name="P", vendor_id=v.id); s.add(p); await s.flush()
        src = DocumentationSource(name="S", base_url="https://s", product_id=p.id, source_type="web")
        s.add(src); await s.flush()
        arts = [Article(source_id=src.id, title=str(i), source_url=f"https://s/{i}",
                        topic_key=f"https://s/{i}", content_markdown=f"#{i}", content_hash=f"h{i}")
                for i in range(n)]
        s.add_all(arts); await s.commit()
        return src.id


def _lines(text):
    return [json.loads(l) for l in text.splitlines() if l]


async def test_bootstrap_start_line_matches_terminal_cursor(ctx):
    c, factory = ctx
    await _seed_n(factory, 3)
    lines = _lines((await c.get("/api/articles/delta")).text)
    assert lines[0].get("control") == "bootstrap_start"
    assert lines[-1].get("control") == "cursor"
    assert lines[0]["next_since"] == lines[-1]["next_since"]
    added = [l for l in lines if l.get("change_type") == "added"]
    assert len(added) == 3


async def test_bootstrap_after_filters_and_is_gapless(ctx):
    c, factory = ctx
    await _seed_n(factory, 3)
    full = [l for l in _lines((await c.get("/api/articles/delta")).text) if l.get("change_type") == "added"]
    ids = [l["id"] for l in full]                       # ascending by Article.id
    rest = [l for l in _lines((await c.get(f"/api/articles/delta?bootstrap_after={ids[0]}")).text)
            if l.get("change_type") == "added"]
    assert [l["id"] for l in rest] == ids[1:]           # first one skipped, no dupes/gaps


async def test_resume_with_original_watermark_catches_missed_update(ctx):
    """Using the FIRST attempt's watermark (not the resume-time one) means an
    update to an already-emitted article is still served by incremental."""
    c, factory = ctx
    src_id = await _seed_n(factory, 2)
    lines = _lines((await c.get("/api/articles/delta")).text)
    x = lines[0]["next_since"]                          # first-attempt watermark
    added = [l for l in lines if l.get("change_type") == "added"]
    a0 = added[0]["id"]

    # Update the already-emitted first article (writes a content_changes row).
    async with factory() as s:
        art = (await s.execute(select(Article).where(Article.id == a0))).scalar_one()
        art.content_markdown = "# changed"
        from app.services import change_log
        # simulate an extraction updated-row for this article
        await change_log.record_change(s, article=art, change_type="updated", run_id=None)
        await s.commit()

    # Resume bootstrap past a0 (recomputed watermark would MISS the update)...
    _resume = await c.get(f"/api/articles/delta?bootstrap_after={a0}")
    # ...but incremental from the ORIGINAL watermark x still serves the update.
    inc = _lines((await c.get(f"/api/articles/delta?since={x}")).text)
    updated_ids = [l["id"] for l in inc if l.get("change_type") == "updated"]
    assert a0 in updated_ids
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && ./venv/bin/pytest tests/test_bootstrap_resume.py -v`
Expected: FAIL — no `bootstrap_start` line; `bootstrap_after` is an unknown query param (ignored) so filtering doesn't happen.

- [ ] **Step 3: Implement — `stream_bootstrap`** (`backend/app/services/delta_feed.py`)

Add the `bootstrap_after` parameter, the start-line emission, and the keyset filter:

```python
async def stream_bootstrap(
    db: AsyncSession, *, source_id, vendor_id, visible_vendor_ids, bootstrap_after=None
) -> AsyncIterator[str]:
    max_seq = (await db.execute(select(func.max(ContentChange.id)))).scalar() or 0
    ceiling = await _safe_ceiling(db)
    if ceiling is not None:
        max_seq = min(max_seq, ceiling - 1)
    cursor = encode_delta_cursor(max_seq)
    # Deliver the watermark up front so a consumer whose stream drops mid-bootstrap
    # can resume (bootstrap_after) while anchoring incremental to THIS watermark.
    yield _line({"control": "bootstrap_start", "next_since": cursor})
    resolver = ChapterResolver()
    last_id = bootstrap_after
    count = 0

    while True:
        conds = [Article.removed_at.is_(None)]
        if source_id is not None:
            conds.append(Article.source_id == source_id)
        if vendor_id is not None:
            conds.append(Article.source_id.in_(_vendor_sources_subq(vendor_id)))
        if visible_vendor_ids is not None:
            conds.append(Article.source_id.in_(_visible_sources_subq(visible_vendor_ids)))
        if last_id is not None:
            conds.append(Article.id > last_id)
        # ... (unchanged select/limit/order_by Article.id, emit loop) ...
```

Keep the rest of the loop identical, and the terminal line unchanged:

```python
    yield _line({"control": "cursor", "next_since": cursor, "count": count})
```

(Reuse the single `cursor` computed at the top for both the start and terminal lines — do not recompute.)

- [ ] **Step 4: Implement — route param** (`backend/app/routes/articles.py`)

Add the query param and forward it:

```python
    bootstrap_after: uuid.UUID | None = Query(
        None, description="Bootstrap only: resume after this article id (ignored when 'since' is set)."
    ),
```

and in the `since is None` branch:

```python
        gen = delta_feed.stream_bootstrap(
            db, source_id=source_id, vendor_id=vendor_id, visible_vendor_ids=visible,
            bootstrap_after=bootstrap_after,
        )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend && ./venv/bin/pytest tests/test_bootstrap_resume.py -v`
Expected: all PASS.

- [ ] **Step 6: Document** — `docs/API.md`

In the Delta-Feed section, document `bootstrap_after`, the `bootstrap_start` control line, and the resume contract: capture `next_since` from `bootstrap_start` up front; on a dropped stream resume with `?bootstrap_after=<highest id applied>` and keep the original `next_since`; only start incremental after the terminal `cursor` line.

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/delta_feed.py backend/app/routes/articles.py docs/API.md backend/tests/test_bootstrap_resume.py
git commit -m "feat(delta): resumable bootstrap (bootstrap_after + bootstrap_start watermark)"
```

---

### Task 5: Documentation pass

**Files:**
- Modify: `docs/API.md` (Delta-Feed section), and `docs/ARCHITECTURE.md` if it describes the outbox/feed.

**Interfaces:**
- Consumes: the *implemented* behavior of Tasks 1–4 (documentation is written last, against the code that actually shipped, not the plan's prose).

> **Note:** Tasks 3 and 4 SKIP their inline documentation sub-steps (T3 step 7, T4 step 6). All delta-feed documentation is consolidated here so it's written once against the final code.

- [ ] **Step 1: Update the `docs/API.md` Delta-Feed section — deletion tombstones**

Document that deleting a source/product/vendor now emits `removed` tombstones carrying the original article `id` (verified against `record_source_deletions` + the deletion routes as implemented), so a consumer that processes `removed` records drops those nodes instead of orphaning them. Note the tombstones use `run_id: null`.

- [ ] **Step 2: Update the `docs/API.md` Delta-Feed section — resumable bootstrap**

Document the `bootstrap_after=<article_id>` query parameter (bootstrap-mode only; ignored when `since` is set), the new first-line `{"control":"bootstrap_start","next_since":...}` record, and the resume contract: capture `next_since` from `bootstrap_start` up front; on a dropped stream resume with `?bootstrap_after=<highest id applied>` while KEEPING the original `next_since`; only begin incremental (`since=`) after the terminal `{"control":"cursor",...}` line. Match the exact param name, control type, and field names to the shipped code in `delta_feed.py`/`articles.py`.

- [ ] **Step 3: Cross-check against code**

Read `app/services/delta_feed.py` (`stream_bootstrap`), `app/routes/articles.py` (`article_delta_feed`), `app/services/change_log.py` (`record_source_deletions`), and the three deletion routes; confirm every param name, control-record type, and field the docs mention exists exactly as written. Fix any drift.

- [ ] **Step 4: Commit**

```bash
git add docs/API.md docs/ARCHITECTURE.md
git commit -m "docs(api): delta-feed deletion tombstones + resumable bootstrap"
```

---

## Self-Review

**Spec coverage:** Part 1 §1.1 → Task 1; §1.2 → Task 2; §1.3–1.4 → Task 3; Part 2 → Task 4; all documentation → Task 5 (Tasks 3 & 4 skip their inline doc steps); every spec test bullet maps to a test step (append-only survive-delete → T1; helper live-only → T2; source/product/vendor tombstones + safe-ceiling → T3; bootstrap_start/after/missed-update → T4). Retention is spec'd out-of-scope; not planned. ✅

**Placeholder scan:** migration `<new>` filename is the Alembic-generated name (instructed to generate it); constraint names use the PostgreSQL default with a fallback instruction. No other placeholders. ✅

**Type consistency:** `record_removals(... run_id: uuid.UUID | None)` and `record_source_deletions(db, *, source_ids) -> int` are used identically in Tasks 2→3; `stream_bootstrap(..., bootstrap_after=None)` matches the route call in Task 4; the `bootstrap_start`/`cursor` control lines share one `cursor` value. ✅

**Note for executor:** if `test_change_log.py` lacks a reusable async `factory` fixture, add one mirroring Task 1's `ctx` (yield only `factory`). Confirm the migration `down_revision`/head and the real FK constraint names before finalizing Task 1's migration.
