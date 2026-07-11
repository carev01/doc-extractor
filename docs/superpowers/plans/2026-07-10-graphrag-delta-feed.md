# GraphRAG Delta Feed Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the downstream GraphRAG pipeline a pull-based, gap-free delta feed of article changes (JSONL over HTTP) plus a webhook that nudges it to pull after each run.

**Architecture:** An append-only `content_changes` outbox table (BIGSERIAL `id` = the watermark) gets one row per article add/update/remove, written in the same transaction as the mutation during extraction. A streaming `GET /api/articles/delta` endpoint serves those rows as JSONL, ordered by `id`, using a "safe ceiling" (the lowest `id` belonging to any still-active run) so a slow concurrent run can never be skipped. The existing `extraction_complete` webhook gains a `delta` summary block so the downstream knows to pull.

**Tech Stack:** FastAPI, SQLAlchemy (async/asyncpg in app, sync/psycopg2 in tests), Alembic, Pydantic v2, PostgreSQL, httpx.

## Global Constraints

- Settings use the `DOCEXTRACTOR_` prefix (pydantic-settings).
- **Every new model must be imported in `app/models/__init__.py`** before `Base.metadata.create_all` runs, and added to its `__all__`.
- Extraction always passes a pre-created `run_id`; never create a second run row.
- Best-effort side channels (webhooks) must never raise into the extraction path.
- Tests run against the `docextractor_test` database. Data-layer tests use sync `psycopg2` + `Session`; async routes are tested with `httpx.AsyncClient` + `ASGITransport` (see `tests/test_enhanced_search.py` for the canonical fixture).
- Commit trailer on every commit:
  ```
  Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01LZoiNMkURTEexS4UEY8rF4
  ```
- Reference spec: `docs/superpowers/specs/2026-07-10-graphrag-delta-feed-design.md`.

---

## File Structure

- `app/models/content_change.py` — **create** — the `ContentChange` ORM model + `ChangeType` enum.
- `app/models/__init__.py` — **modify** — register `ContentChange`, `ChangeType`.
- `alembic/versions/h2b3c4d5e6f7_add_content_changes.py` — **create** — migration for the table.
- `tests/test_defects.py` — **modify** — update the table-name invariant (adds `content_changes`).
- `app/services/change_log.py` — **create** — write helpers (`record_change`, `record_removals`) + `run_change_counts`.
- `tests/test_change_log.py` — **create** — sync unit tests for the write helpers + counts.
- `app/services/firecrawl.py` — **modify** — call the write helpers at the add/update site (`process_article_result`) and the removal site (`_reconcile_removals`); enrich the `extraction_complete` webhook payload.
- `tests/test_change_log_wiring.py` — **create** — async tests that the extraction sites emit outbox rows.
- `app/schemas/delta.py` — **create** — delta cursor codec + JSONL record shapes (documented; the stream emits plain dicts).
- `app/services/delta_feed.py` — **create** — the streaming generators (`stream_delta`, `stream_bootstrap`), safe-ceiling logic, record builders, `ChapterResolver`.
- `app/routes/articles.py` — **modify** — add `GET /api/articles/delta` **before** the `/{article_id}` route.
- `tests/test_delta_feed.py` — **create** — endpoint integration tests (bootstrap, delta, tombstones, safe-ceiling, RBAC, invalid cursor).

---

## Task 1: `ContentChange` model, migration, registration

**Files:**
- Create: `app/models/content_change.py`
- Modify: `app/models/__init__.py`
- Create: `alembic/versions/h2b3c4d5e6f7_add_content_changes.py`
- Modify: `tests/test_defects.py:56-75`
- Test: `tests/test_defects.py::test_defect1_all_tables_in_metadata`

**Interfaces:**
- Produces: `ContentChange` (table `content_changes`) with columns `id: int` (BIGSERIAL PK), `article_id: uuid|None`, `source_id: uuid|None`, `run_id: uuid|None`, `change_type: str`, `content_hash: str|None`, `topic_key: str|None`, `created_at: datetime`. Enum `ChangeType` with values `ADDED="added"`, `UPDATED="updated"`, `REMOVED="removed"`.

- [ ] **Step 1: Update the failing table-name invariant test**

In `tests/test_defects.py`, add `"content_changes"` to the sorted list (alphabetical: after `auth_realms`, before `documentation_sources`) and bump the count message.

```python
    table_names = sorted(Base.metadata.tables.keys())
    assert table_names == [
        "api_keys",
        "article_images",
        "article_versions",
        "articles",
        "auth_realms",
        "content_changes",
        "documentation_sources",
        "export_jobs",
        "extraction_runs",
        "job_runs",
        "jobs",
        "products",
        "toc_checkpoints",
        "toc_entries",
        "user_vendor_permissions",
        "users",
        "vendors",
        "webhook_deliveries",
        "webhooks",
    ], f"Expected 19 tables, got {len(table_names)}: {table_names}"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest tests/test_defects.py::test_defect1_all_tables_in_metadata -v`
Expected: FAIL — `content_changes` missing from `Base.metadata` (model not defined/registered yet).

- [ ] **Step 3: Create the model**

Create `app/models/content_change.py`:

```python
"""ContentChange model — append-only outbox of article change events.

One row is written, in the same transaction as the mutation, whenever an
article is added, updated, or removed during extraction. The BIGSERIAL ``id``
is the monotonic watermark the delta feed pages by; no timestamp drives
ordering. ``topic_key`` is copied onto the row so a removal tombstone stays
resolvable even after the article row is later hard-deleted.
"""

import uuid
from datetime import datetime
from enum import Enum

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ChangeType(str, Enum):
    ADDED = "added"
    UPDATED = "updated"
    REMOVED = "removed"


class ContentChange(Base):
    __tablename__ = "content_changes"

    __table_args__ = (
        # Safe-ceiling and run-summary lookups scan by run_id.
        Index("ix_content_changes_run_id", "run_id"),
        # Source-scoped feed scans page by (source_id, id).
        Index("ix_content_changes_source_id_id", "source_id", "id"),
    )

    # BIGSERIAL on Postgres — the monotonic watermark.
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    article_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("articles.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documentation_sources.id", ondelete="SET NULL"),
        nullable=True,
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("extraction_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    change_type: Mapped[str] = mapped_column(String(16), nullable=False)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    topic_key: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
```

- [ ] **Step 4: Register the model**

In `app/models/__init__.py`, add the import after the webhook import and the names to `__all__`:

```python
from app.models.content_change import ContentChange, ChangeType
```

Add `"ContentChange"` and `"ChangeType"` to the `__all__` list.

- [ ] **Step 5: Run the invariant test to verify it passes**

Run: `pytest tests/test_defects.py::test_defect1_all_tables_in_metadata -v`
Expected: PASS.

- [ ] **Step 6: Create the Alembic migration**

Confirm the current head first: `cd backend && alembic heads` — it must print exactly `a3c1e5b7d9f2 (head)` (the real DAG head; do not assume from file mtime). Create `alembic/versions/h2b3c4d5e6f7_add_content_changes.py` chaining from it:

```python
"""add content_changes outbox

Revision ID: h2b3c4d5e6f7
Revises: a3c1e5b7d9f2
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "h2b3c4d5e6f7"
down_revision: Union[str, Sequence[str], None] = "a3c1e5b7d9f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "content_changes",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "article_id",
            UUID(as_uuid=True),
            sa.ForeignKey("articles.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "source_id",
            UUID(as_uuid=True),
            sa.ForeignKey("documentation_sources.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("extraction_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("change_type", sa.String(16), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=True),
        sa.Column("topic_key", sa.String(2048), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_content_changes_run_id", "content_changes", ["run_id"])
    op.create_index("ix_content_changes_source_id_id", "content_changes", ["source_id", "id"])


def downgrade() -> None:
    op.drop_index("ix_content_changes_source_id_id", table_name="content_changes")
    op.drop_index("ix_content_changes_run_id", table_name="content_changes")
    op.drop_table("content_changes")
```

- [ ] **Step 7: Verify the migration applies cleanly**

Run: `cd backend && alembic upgrade head && alembic heads`
Expected: the one new migration applies without error; `alembic heads` prints exactly one line, `h2b3c4d5e6f7 (head)` (a single head — a second head means `down_revision` is wrong). The dev DB is already at `a3c1e5b7d9f2`, so only `content_changes` is created.

- [ ] **Step 8: Commit**

```bash
git add backend/app/models/content_change.py backend/app/models/__init__.py \
        backend/alembic/versions/h2b3c4d5e6f7_add_content_changes.py backend/tests/test_defects.py
git commit -m "feat(delta): add content_changes outbox model + migration"
```

---

## Task 2: `change_log` write helpers + run counts

**Files:**
- Create: `app/services/change_log.py`
- Test: `tests/test_change_log.py`

**Interfaces:**
- Consumes: `ContentChange`, `ChangeType` (Task 1); `Article` (has `.id`, `.source_id`, `.topic_key`, `.content_hash`).
- Produces:
  - `async def record_change(db: AsyncSession, *, article: Article, change_type: str, run_id: uuid.UUID) -> None` — adds one `ContentChange` row to the session (caller commits).
  - `async def record_removals(db: AsyncSession, *, rows: list, source_id: uuid.UUID, run_id: uuid.UUID) -> None` — adds a `removed` row per element; each element has `.id` and `.topic_key` attributes.
  - `async def run_change_counts(db: AsyncSession, run_id: uuid.UUID) -> dict[str, int]` — returns `{"added": int, "updated": int, "removed": int}` for a run.

- [ ] **Step 1: Write the failing test**

Create `tests/test_change_log.py`:

```python
"""Unit tests for the content_changes write helpers (sync psycopg2 session)."""
import asyncio
import os
import sys
import uuid

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core.database import Base
from app.models import (
    Vendor, Product, DocumentationSource, Article, ExtractionRun,
)
from app.models.content_change import ContentChange
from app.services import change_log

TEST_DATABASE_URL_SYNC = settings.database_url_sync.rsplit("/", 1)[0] + "/docextractor_test"
sync_engine = create_engine(TEST_DATABASE_URL_SYNC, echo=False)
SyncSession = sessionmaker(sync_engine, class_=Session, expire_on_commit=False)


@pytest.fixture(scope="function")
def db_session():
    Base.metadata.drop_all(sync_engine)
    Base.metadata.create_all(sync_engine)
    session = SyncSession()
    yield session
    session.rollback()
    session.close()


def _seed(session):
    vendor = Vendor(name="V")
    session.add(vendor); session.flush()
    product = Product(name="P", vendor_id=vendor.id)
    session.add(product); session.flush()
    source = DocumentationSource(name="S", base_url="https://x", product_id=product.id)
    session.add(source); session.flush()
    run = ExtractionRun(source_id=source.id)
    session.add(run); session.flush()
    return source, run


def test_record_change_writes_row(db_session):
    source, run = _seed(db_session)
    article = Article(
        source_id=source.id, extraction_run_id=run.id, created_run_id=run.id,
        title="T", source_url="https://x/a", topic_key="https://x/a",
        content_markdown="# T", content_hash="hash-abc",
    )
    db_session.add(article); db_session.flush()

    # record_change is a coroutine but performs no awaited I/O — it only calls
    # db.add — so asyncio.run drives it to completion against a sync Session.
    # (asyncio.run, not get_event_loop().run_until_complete, to avoid the 3.12
    # "no current event loop" DeprecationWarning — test output must stay pristine.)
    asyncio.run(
        change_log.record_change(db_session, article=article, change_type="added", run_id=run.id)
    )
    db_session.commit()

    rows = db_session.execute(select(ContentChange)).scalars().all()
    assert len(rows) == 1
    assert rows[0].change_type == "added"
    assert rows[0].article_id == article.id
    assert rows[0].source_id == source.id
    assert rows[0].run_id == run.id
    assert rows[0].content_hash == "hash-abc"
    assert rows[0].topic_key == "https://x/a"
    assert rows[0].id >= 1  # BIGSERIAL assigned
```

> Note: `record_change` uses only `db.add(...)` (no `await` on I/O), so passing a sync `Session` works. This keeps the helper testable without an async harness.

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest tests/test_change_log.py::test_record_change_writes_row -v`
Expected: FAIL — `app.services.change_log` does not exist.

- [ ] **Step 3: Implement the service**

Create `app/services/change_log.py`:

```python
"""Write helpers for the content_changes outbox + per-run change counts.

record_change / record_removals only ``db.add`` rows to the caller's session;
they do not commit, so the outbox row lands in the same transaction as the
article mutation that triggered it.
"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article import Article
from app.models.content_change import ChangeType, ContentChange


async def record_change(
    db: AsyncSession, *, article: Article, change_type: str, run_id: uuid.UUID
) -> None:
    """Append one added/updated outbox row for *article* (caller commits)."""
    db.add(
        ContentChange(
            article_id=article.id,
            source_id=article.source_id,
            run_id=run_id,
            change_type=change_type,
            content_hash=article.content_hash,
            topic_key=article.topic_key,
        )
    )


async def record_removals(
    db: AsyncSession, *, rows, source_id: uuid.UUID, run_id: uuid.UUID
) -> None:
    """Append a ``removed`` outbox row per element (each has .id, .topic_key)."""
    for r in rows:
        db.add(
            ContentChange(
                article_id=r.id,
                source_id=source_id,
                run_id=run_id,
                change_type=ChangeType.REMOVED.value,
                content_hash=None,
                topic_key=r.topic_key,
            )
        )


async def run_change_counts(db: AsyncSession, run_id: uuid.UUID) -> dict[str, int]:
    """Return {'added': n, 'updated': n, 'removed': n} for a run's outbox rows."""
    stmt = (
        select(ContentChange.change_type, func.count())
        .where(ContentChange.run_id == run_id)
        .group_by(ContentChange.change_type)
    )
    counts = {"added": 0, "updated": 0, "removed": 0}
    for change_type, n in (await db.execute(stmt)).all():
        counts[change_type] = n
    return counts
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/test_change_log.py::test_record_change_writes_row -v`
Expected: PASS.

- [ ] **Step 5: Add a test for removals**

Append to `tests/test_change_log.py`. Assert the removed rows directly (query `ContentChange`) — do **not** call `run_change_counts` here: it does `await db.execute(...)`, which fails on a sync `Session` (that helper is covered by Task 7's async test). `record_removals`, like `record_change`, only calls `db.add`, so `asyncio.run` drives it cleanly.

```python
def test_record_removals_writes_rows(db_session):
    source, run = _seed(db_session)

    class Row:
        def __init__(self, id, topic_key):
            self.id = id
            self.topic_key = topic_key

    removed = [Row(uuid.uuid4(), "https://x/gone1"), Row(uuid.uuid4(), "https://x/gone2")]
    asyncio.run(
        change_log.record_removals(db_session, rows=removed, source_id=source.id, run_id=run.id)
    )
    db_session.commit()

    rows = db_session.execute(
        select(ContentChange).where(ContentChange.change_type == "removed").order_by(ContentChange.id)
    ).scalars().all()
    assert len(rows) == 2
    assert {r.topic_key for r in rows} == {"https://x/gone1", "https://x/gone2"}
    assert all(r.source_id == source.id and r.run_id == run.id for r in rows)
    assert all(r.content_hash is None for r in rows)
    assert {r.article_id for r in rows} == {removed[0].id, removed[1].id}
```

> `run_change_counts` is implemented in Step 3 (Task 7 needs it) and is exercised by `tests/test_delta_webhook.py::test_run_change_counts_feeds_delta_block` in Task 7's async harness — the only place a sync/async mismatch is avoided.

- [ ] **Step 6: Run both tests**

Run: `pytest tests/test_change_log.py -v`
Expected: 2 passed, output pristine (no deprecation warnings).

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/change_log.py backend/tests/test_change_log.py
git commit -m "feat(delta): content_changes write helpers + run counts"
```

---

## Task 3: Wire outbox writes into extraction

**Files:**
- Modify: `app/services/firecrawl.py` (add/update site ~line 979–994; removal site ~line 1584–1620)
- Test: `tests/test_change_log_wiring.py`

**Interfaces:**
- Consumes: `change_log.record_change`, `change_log.record_removals` (Task 2).
- Produces: outbox rows written transactionally during extraction. No new public symbols.

- [ ] **Step 1: Write the failing async wiring tests**

Create `tests/test_change_log_wiring.py`:

```python
"""The extraction persistence sites emit content_changes rows.

Async httpx-style harness (async session against docextractor_test), because
process_article_result and _reconcile_removals are async and use db.commit().
"""
import os
import sys
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core.database import Base
from app.models import Vendor, Product, DocumentationSource, ExtractionRun, Article
from app.models.toc import TOCEntry
from app.models.content_change import ContentChange
from app.services.firecrawl import FirecrawlService

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with factory() as s:
        yield s
    await engine.dispose()


async def _seed(s):
    vendor = Vendor(name="V"); s.add(vendor); await s.flush()
    product = Product(name="P", vendor_id=vendor.id); s.add(product); await s.flush()
    source = DocumentationSource(name="S", base_url="https://x", product_id=product.id)
    s.add(source); await s.flush()
    run = ExtractionRun(source_id=source.id); s.add(run); await s.flush()
    await s.commit()
    return source, run


async def test_new_article_emits_added_change(session):
    source, run = await _seed(session)
    svc = FirecrawlService()
    outcome = await svc.process_article_result(
        session, source_id=source.id, run_id=run.id,
        url="https://x/a", markdown_content="# A\n\nBody text here.",
        doc_html="", toc_entry_id=None, sort_order=0, title="A",
    )
    assert outcome == "new"
    rows = (await session.execute(select(ContentChange))).scalars().all()
    assert len(rows) == 1
    assert rows[0].change_type == "added"
    assert rows[0].run_id == run.id


async def test_changed_article_emits_updated_change(session):
    source, run = await _seed(session)
    svc = FirecrawlService()
    await svc.process_article_result(
        session, source_id=source.id, run_id=run.id, url="https://x/a",
        markdown_content="# A\n\nfirst.", doc_html="", toc_entry_id=None,
        sort_order=0, title="A",
    )
    await svc.process_article_result(
        session, source_id=source.id, run_id=run.id, url="https://x/a",
        markdown_content="# A\n\nSECOND, changed.", doc_html="", toc_entry_id=None,
        sort_order=0, title="A",
    )
    types = [r.change_type for r in (await session.execute(
        select(ContentChange).order_by(ContentChange.id))).scalars().all()]
    assert types == ["added", "updated"]


async def test_reconcile_removals_emits_removed_change(session):
    source, run = await _seed(session)
    # An article whose TOC entry is gone (toc_entry_id NULL, url not in TOC) → removed.
    art = Article(
        source_id=source.id, extraction_run_id=run.id, created_run_id=run.id,
        title="Gone", source_url="https://x/gone", topic_key="https://x/gone",
        content_markdown="# Gone", content_hash="h", toc_entry_id=None,
    )
    session.add(art); await session.commit()
    svc = FirecrawlService()
    await svc._reconcile_removals(session, source.id, run.id)
    rows = (await session.execute(
        select(ContentChange).where(ContentChange.change_type == "removed"))).scalars().all()
    assert len(rows) == 1
    assert rows[0].article_id == art.id
    assert rows[0].topic_key == "https://x/gone"
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_change_log_wiring.py -v`
Expected: FAIL — no `content_changes` rows written (wiring absent).

- [ ] **Step 3: Wire the add/update site**

In `app/services/firecrawl.py`, add the import near the other service imports at the top of the file:

```python
from app.services import change_log
```

In `process_article_result`, immediately after `article.content_markdown = markdown_content` (currently line 979) and before the counter-increment block, insert:

```python
        # Outbox: record the change in the same transaction as the mutation.
        await change_log.record_change(
            db,
            article=article,
            change_type="added" if outcome == "new" else "updated",
            run_id=run_id,
        )
```

(The existing `await db.commit()` a few lines below now also commits this row.)

- [ ] **Step 4: Wire the removal site**

In `_reconcile_removals`, the `newly_removed` query is currently gated on a webhook subscriber. Change it to **always** run and to also select `topic_key`. Replace the `notify_removed` / `newly_removed` block (currently lines ~1587–1600) with:

```python
        # Always capture the newly-removed rows: needed for the outbox, and reused
        # for the removed_page webhook payloads when a subscriber exists.
        newly_removed = (
            await db.execute(
                select(
                    Article.id, Article.title, Article.source_url, Article.topic_key
                ).where(
                    Article.source_id == source_id,
                    Article.toc_entry_id.is_(None),
                    Article.removed_at.is_(None),
                )
            )
        ).all()
```

Then, after the `update(...).values(removed_at=now, removal_run_id=run_id)` statement and before `await db.commit()` (currently line 1620), insert:

```python
        # Outbox: one removed row per newly-removed article, same transaction.
        if newly_removed:
            await change_log.record_removals(
                db, rows=newly_removed, source_id=source_id, run_id=run_id
            )
```

The `removed_page` webhook loop below stays as-is (it reads `row.source_url` / `row.title`, both still selected). Remove the now-unused `notify_removed` variable if the linter flags it; the webhook loop can gate itself with `if webhook_dispatcher.run_has_subscribers(run_id, "removed_page"):` around the `for row in newly_removed:` loop.

- [ ] **Step 5: Run the wiring tests to verify they pass**

Run: `pytest tests/test_change_log_wiring.py -v`
Expected: 3 passed.

- [ ] **Step 6: Run the existing extraction/removal tests for regressions**

Run: `pytest tests/test_incremental.py tests/test_reconcile_removals.py tests/test_webhooks.py -v`
Expected: all pass (no behavior change to existing paths beyond the added rows).

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/firecrawl.py backend/tests/test_change_log_wiring.py
git commit -m "feat(delta): write content_changes rows on add/update/remove during extraction"
```

---

## Task 4: Delta cursor codec + record shapes

**Files:**
- Create: `app/schemas/delta.py`
- Test: `tests/test_delta_cursor.py`

**Interfaces:**
- Consumes: `encode_cursor`, `decode_cursor` from `app/schemas/search.py`.
- Produces:
  - `def encode_delta_cursor(seq: int) -> str`
  - `def decode_delta_cursor(cursor: str) -> int` — raises `ValueError` on a malformed token or wrong version.
  - Documented record shapes (as reference; the feed emits plain dicts): `CONTENT_FIELDS`, `TOMBSTONE_FIELDS` docstrings.

- [ ] **Step 1: Write the failing test**

Create `tests/test_delta_cursor.py`:

```python
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.schemas.delta import encode_delta_cursor, decode_delta_cursor


def test_roundtrip():
    token = encode_delta_cursor(4811)
    assert decode_delta_cursor(token) == 4811


def test_zero():
    assert decode_delta_cursor(encode_delta_cursor(0)) == 0


def test_malformed_raises():
    with pytest.raises(ValueError):
        decode_delta_cursor("not-a-real-cursor!!")


def test_wrong_version_raises():
    from app.schemas.search import encode_cursor
    bad = encode_cursor({"v": 999, "seq": 5})
    with pytest.raises(ValueError):
        decode_delta_cursor(bad)
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_delta_cursor.py -v`
Expected: FAIL — `app.schemas.delta` does not exist.

- [ ] **Step 3: Implement the codec**

Create `app/schemas/delta.py`:

```python
"""Delta-feed cursor codec + JSONL record field reference.

The cursor is an opaque, versioned wrapper over the content_changes watermark
(BIGSERIAL id). It reuses the base64-JSON codec from schemas.search so all
opaque cursors in the app share one encoding.

Record shapes emitted by the feed (as plain dicts, one JSON object per line):

CONTENT (change_type "added" | "updated"):
    seq, change_type, id, topic_key, source_id, vendor, product, title,
    source_url, last_updated_at, content_hash, estimated_tokens,
    parent_chapter, top_level_chapter, sort_order, run_id,
    content_markdown, images:[{url, alt, description, kind}]
    (In bootstrap mode seq is null — snapshot rows are not change events.)

TOMBSTONE (change_type "removed"):
    seq, change_type, id, topic_key, source_id, removed_at, run_id

CONTROL (always last line):
    control:"cursor", next_since, count
"""

from app.schemas.search import decode_cursor, encode_cursor

_CURSOR_VERSION = 1


def encode_delta_cursor(seq: int) -> str:
    """Encode a watermark seq as an opaque delta cursor."""
    return encode_cursor({"v": _CURSOR_VERSION, "seq": int(seq)})


def decode_delta_cursor(cursor: str) -> int:
    """Decode a delta cursor to its watermark seq.

    Raises ValueError if the token is malformed or not version 1.
    """
    payload = decode_cursor(cursor)  # raises ValueError on malformed base64/JSON
    if payload.get("v") != _CURSOR_VERSION or "seq" not in payload:
        raise ValueError("Unrecognized delta cursor")
    try:
        return int(payload["seq"])
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid delta cursor seq") from exc
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/test_delta_cursor.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/schemas/delta.py backend/tests/test_delta_cursor.py
git commit -m "feat(delta): opaque delta cursor codec"
```

---

## Task 5: `delta_feed` service — safe ceiling, record builders, streaming generators

**Files:**
- Create: `app/services/delta_feed.py`
- Test: covered end-to-end by Task 6 (the streaming generators need a live DB + join; unit-level correctness of the record builder is asserted there via the endpoint).

**Interfaces:**
- Consumes: `ContentChange`, `Article`, `TOCEntry`, `ArticleImage`, `DocumentationSource`, `Product`, `Vendor`, `ExtractionRun`, `RunStatus`; `encode_delta_cursor`.
- Produces:
  - `class ChapterResolver` with `async def resolve(self, db, source_id, toc_entry_id) -> tuple[str | None, str | None]` returning `(parent_title, top_level_title)`, memoized per source.
  - `async def stream_delta(db, *, since_seq: int, source_id, vendor_id, visible_vendor_ids) -> AsyncIterator[str]` — yields JSONL lines then a control line.
  - `async def stream_bootstrap(db, *, source_id, vendor_id, visible_vendor_ids) -> AsyncIterator[str]` — yields JSONL `added` snapshot lines then a control line.
  - `_BATCH = 500` (internal keyset batch size).

- [ ] **Step 1: Implement the service**

Create `app/services/delta_feed.py`:

```python
"""Streaming JSONL delta feed over the content_changes outbox.

Ordering is by content_changes.id alone. Gap-freeness under concurrent runs is
guaranteed by a "safe ceiling": the lowest id belonging to any still-active run.
Because an active run's outbox rows all have id >= that run's minimum id, every
row with id < ceiling provably belongs to a terminal run, so serving strictly
below the ceiling never skips a slow run's not-yet-committed higher ids.
"""

import json
import uuid
from typing import AsyncIterator

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.article import Article
from app.models.content_change import ContentChange
from app.models.extraction_run import ExtractionRun, RunStatus
from app.models.product import Product
from app.models.source import DocumentationSource
from app.models.toc import TOCEntry
from app.models.vendor import Vendor
from app.schemas.delta import encode_delta_cursor

_BATCH = 500
_ACTIVE = (RunStatus.PENDING, RunStatus.RUNNING, RunStatus.PAUSED)


class ChapterResolver:
    """Resolves (parent_title, top_level_title) for an article's TOC entry,
    loading each source's TOC once and caching it for the stream's lifetime."""

    def __init__(self) -> None:
        self._by_source: dict[uuid.UUID, dict] = {}

    async def _load(self, db: AsyncSession, source_id: uuid.UUID) -> dict:
        cached = self._by_source.get(source_id)
        if cached is not None:
            return cached
        rows = (
            await db.execute(
                select(TOCEntry.id, TOCEntry.parent_id, TOCEntry.title).where(
                    TOCEntry.source_id == source_id
                )
            )
        ).all()
        parent_of = {r.id: r.parent_id for r in rows}
        title_of = {r.id: r.title for r in rows}
        entry = {"parent_of": parent_of, "title_of": title_of}
        self._by_source[source_id] = entry
        return entry

    async def resolve(self, db, source_id, toc_entry_id):
        if source_id is None or toc_entry_id is None:
            return None, None
        m = await self._load(db, source_id)
        parent_of, title_of = m["parent_of"], m["title_of"]
        pid = parent_of.get(toc_entry_id)
        parent_title = title_of.get(pid) if pid is not None else None
        root = toc_entry_id
        seen: set[uuid.UUID] = set()
        while parent_of.get(root) is not None and root not in seen:
            seen.add(root)
            root = parent_of[root]
        top_title = title_of.get(root)
        return parent_title, top_title


async def _safe_ceiling(db: AsyncSession) -> int | None:
    """Lowest content_changes.id belonging to a still-active run, or None."""
    stmt = (
        select(func.min(ContentChange.id))
        .join(ExtractionRun, ExtractionRun.id == ContentChange.run_id)
        .where(ExtractionRun.status.in_(_ACTIVE))
    )
    return (await db.execute(stmt)).scalar()


def _visible_sources_subq(visible_vendor_ids):
    return (
        select(DocumentationSource.id)
        .join(Product, DocumentationSource.product_id == Product.id)
        .where(Product.vendor_id.in_(visible_vendor_ids))
        .scalar_subquery()
    )


def _vendor_sources_subq(vendor_id):
    return (
        select(DocumentationSource.id)
        .join(Product, DocumentationSource.product_id == Product.id)
        .where(Product.vendor_id == vendor_id)
        .scalar_subquery()
    )


def _images_payload(article: Article) -> list[dict]:
    return [
        {
            "url": img.local_path,
            "alt": img.alt_text,
            "description": getattr(img, "description", None),
            "kind": getattr(img, "kind", None),
        }
        for img in sorted(article.images, key=lambda i: i.sort_order)
    ]


async def _content_record(db, resolver, *, seq, change_type, article, vendor_name, product_name):
    parent_title, top_title = await resolver.resolve(db, article.source_id, article.toc_entry_id)
    return {
        "seq": seq,
        "change_type": change_type,
        "id": str(article.id),
        "topic_key": article.topic_key,
        "source_id": str(article.source_id),
        "vendor": vendor_name,
        "product": product_name,
        "title": article.title,
        "source_url": article.source_url,
        "last_updated_at": article.last_updated_at.isoformat() if article.last_updated_at else None,
        "content_hash": article.content_hash,
        "estimated_tokens": article.estimated_tokens,
        "parent_chapter": parent_title,
        "top_level_chapter": top_title,
        "sort_order": article.sort_order,
        "run_id": str(article.extraction_run_id) if article.extraction_run_id else None,
        "content_markdown": article.content_markdown,
        "images": _images_payload(article),
    }


def _tombstone_record(change: ContentChange) -> dict:
    return {
        "seq": change.id,
        "change_type": "removed",
        "id": str(change.article_id) if change.article_id else None,
        "topic_key": change.topic_key,
        "source_id": str(change.source_id) if change.source_id else None,
        "removed_at": change.created_at.isoformat() if change.created_at else None,
        "run_id": str(change.run_id) if change.run_id else None,
    }


def _line(obj: dict) -> str:
    return json.dumps(obj, default=str, separators=(",", ":")) + "\n"


async def stream_delta(
    db: AsyncSession, *, since_seq: int, source_id, vendor_id, visible_vendor_ids
) -> AsyncIterator[str]:
    ceiling = await _safe_ceiling(db)
    resolver = ChapterResolver()
    last = since_seq
    count = 0

    while True:
        conds = [ContentChange.id > last]
        if ceiling is not None:
            conds.append(ContentChange.id < ceiling)
        if source_id is not None:
            conds.append(ContentChange.source_id == source_id)
        if vendor_id is not None:
            conds.append(ContentChange.source_id.in_(_vendor_sources_subq(vendor_id)))
        if visible_vendor_ids is not None:
            conds.append(ContentChange.source_id.in_(_visible_sources_subq(visible_vendor_ids)))

        # Outer-join the live article + vendor/product names. Removed rows and
        # rows whose article was hard-deleted have no Article.
        stmt = (
            select(ContentChange, Article, Vendor.name, Product.name)
            .outerjoin(Article, Article.id == ContentChange.article_id)
            .outerjoin(DocumentationSource, DocumentationSource.id == ContentChange.source_id)
            .outerjoin(Product, Product.id == DocumentationSource.product_id)
            .outerjoin(Vendor, Vendor.id == Product.vendor_id)
            .options(selectinload(Article.images))
            .where(*conds)
            .order_by(ContentChange.id)
            .limit(_BATCH)
        )
        rows = (await db.execute(stmt)).all()
        if not rows:
            break
        for change, article, vendor_name, product_name in rows:
            last = change.id
            if change.change_type == "removed":
                yield _line(_tombstone_record(change))
                count += 1
            elif article is not None:
                yield _line(await _content_record(
                    db, resolver, seq=change.id, change_type=change.change_type,
                    article=article, vendor_name=vendor_name, product_name=product_name,
                ))
                count += 1
            # else: added/updated whose article was hard-deleted → skip.
        if len(rows) < _BATCH:
            break

    next_since = encode_delta_cursor(last) if count else encode_delta_cursor(since_seq)
    yield _line({"control": "cursor", "next_since": next_since, "count": count})


async def stream_bootstrap(
    db: AsyncSession, *, source_id, vendor_id, visible_vendor_ids
) -> AsyncIterator[str]:
    # Watermark for the follow-up delta = current global max outbox id (0 if none).
    max_seq = (await db.execute(select(func.max(ContentChange.id)))).scalar() or 0
    resolver = ChapterResolver()
    last_id: uuid.UUID | None = None
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

        stmt = (
            select(Article, Vendor.name, Product.name)
            .join(DocumentationSource, DocumentationSource.id == Article.source_id)
            .join(Product, Product.id == DocumentationSource.product_id)
            .join(Vendor, Vendor.id == Product.vendor_id)
            .options(selectinload(Article.images))
            .where(*conds)
            .order_by(Article.id)
            .limit(_BATCH)
        )
        rows = (await db.execute(stmt)).all()
        if not rows:
            break
        for article, vendor_name, product_name in rows:
            last_id = article.id
            yield _line(await _content_record(
                db, resolver, seq=None, change_type="added",
                article=article, vendor_name=vendor_name, product_name=product_name,
            ))
            count += 1
        if len(rows) < _BATCH:
            break

    yield _line({"control": "cursor", "next_since": encode_delta_cursor(max_seq), "count": count})
```

> Design note (do not remove): the per-row terminal-run check is intentionally **omitted** — the safe ceiling already guarantees every served row (`id < ceiling`) belongs to a terminal run, because any active run's rows have `id >= ceiling`. See the module docstring.

- [ ] **Step 2: Smoke-import the module**

Run: `cd backend && python -c "import app.services.delta_feed"`
Expected: no import error.

- [ ] **Step 3: Commit**

```bash
git add backend/app/services/delta_feed.py
git commit -m "feat(delta): streaming delta_feed service (safe-ceiling, bootstrap, tombstones)"
```

---

## Task 6: `GET /api/articles/delta` endpoint + integration tests

**Files:**
- Modify: `app/routes/articles.py` (add the route **before** `get_article` / the `/{article_id}` route so the literal `delta` path is matched first)
- Test: `tests/test_delta_feed.py`

**Interfaces:**
- Consumes: `stream_delta`, `stream_bootstrap` (Task 5); `decode_delta_cursor` (Task 4); `get_principal`, `Principal` (existing authz).
- Produces: `GET /api/articles/delta` → `StreamingResponse(media_type="application/x-ndjson")`.

- [ ] **Step 1: Write the failing integration tests**

Create `tests/test_delta_feed.py`:

```python
"""Integration tests for GET /api/articles/delta (async httpx client)."""
import json
import os
import sys
import uuid
from datetime import datetime, timezone

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
from app.models.content_change import ContentChange
from app.models.extraction_run import RunStatus
from app.schemas.delta import encode_delta_cursor

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


def _parse(text_body):
    """Split an NDJSON body into (records, control)."""
    lines = [json.loads(l) for l in text_body.splitlines() if l.strip()]
    control = lines[-1]
    assert control.get("control") == "cursor"
    return lines[:-1], control


async def _seed_source(factory, *, run_status=RunStatus.COMPLETED):
    async with factory() as s:
        v = Vendor(name="V"); s.add(v); await s.flush()
        p = Product(name="P", vendor_id=v.id); s.add(p); await s.flush()
        src = DocumentationSource(name="S", base_url="https://x", product_id=p.id)
        s.add(src); await s.flush()
        run = ExtractionRun(source_id=src.id, status=run_status); s.add(run); await s.flush()
        await s.commit()
        return v.id, src.id, run.id


async def _add_article_change(factory, src_id, run_id, *, url, title, change_type="added"):
    async with factory() as s:
        art = Article(
            source_id=src_id, extraction_run_id=run_id, created_run_id=run_id,
            title=title, source_url=url, topic_key=url,
            content_markdown=f"# {title}", content_hash=f"h-{title}",
        )
        s.add(art); await s.flush()
        s.add(ContentChange(
            article_id=art.id, source_id=src_id, run_id=run_id,
            change_type=change_type, content_hash=art.content_hash, topic_key=url,
        ))
        await s.commit()
        return art.id


async def test_bootstrap_streams_all_articles(ctx):
    c, factory = ctx
    _, src_id, run_id = await _seed_source(factory)
    await _add_article_change(factory, src_id, run_id, url="https://x/a", title="A")
    await _add_article_change(factory, src_id, run_id, url="https://x/b", title="B")

    resp = await c.get("/api/articles/delta")  # no since → bootstrap
    assert resp.status_code == 200
    records, control = _parse(resp.text)
    assert {r["title"] for r in records} == {"A", "B"}
    assert all(r["change_type"] == "added" for r in records)
    assert all(r["seq"] is None for r in records)
    assert control["next_since"]  # a cursor to continue from


async def test_delta_since_returns_only_newer(ctx):
    c, factory = ctx
    _, src_id, run_id = await _seed_source(factory)
    await _add_article_change(factory, src_id, run_id, url="https://x/a", title="A")
    # Take a watermark, then add B.
    first = await c.get("/api/articles/delta")
    _, control = _parse(first.text)
    cursor = control["next_since"]
    await _add_article_change(factory, src_id, run_id, url="https://x/b", title="B")

    resp = await c.get(f"/api/articles/delta?since={cursor}")
    records, control2 = _parse(resp.text)
    titles = [r["title"] for r in records]
    assert titles == ["B"]
    assert records[0]["seq"] is not None


async def test_removed_emits_tombstone(ctx):
    c, factory = ctx
    _, src_id, run_id = await _seed_source(factory)
    art_id = await _add_article_change(factory, src_id, run_id, url="https://x/a", title="A")
    async with factory() as s:
        s.add(ContentChange(
            article_id=art_id, source_id=src_id, run_id=run_id,
            change_type="removed", topic_key="https://x/a",
        ))
        await s.commit()

    resp = await c.get(f"/api/articles/delta?since={encode_delta_cursor(0)}")
    records, _ = _parse(resp.text)
    removed = [r for r in records if r["change_type"] == "removed"]
    assert len(removed) == 1
    assert removed[0]["id"] == str(art_id)
    assert removed[0]["topic_key"] == "https://x/a"


async def test_safe_ceiling_withholds_active_run_rows(ctx):
    c, factory = ctx
    # Active (RUNNING) run whose change has a LOW id, plus a COMPLETED run whose
    # change has a HIGHER id. The completed row must be withheld until the running
    # run finishes, because its id sits above the running run's floor.
    _, src_id, running_run = await _seed_source(factory, run_status=RunStatus.RUNNING)
    await _add_article_change(factory, src_id, running_run, url="https://x/live", title="Live")
    async with factory() as s:
        done = ExtractionRun(source_id=src_id, status=RunStatus.COMPLETED)
        s.add(done); await s.flush()
        done_id = done.id
        await s.commit()
    await _add_article_change(factory, src_id, done_id, url="https://x/done", title="Done")

    resp = await c.get(f"/api/articles/delta?since={encode_delta_cursor(0)}")
    records, control = _parse(resp.text)
    # Nothing served: the running run's low id is the ceiling; the completed row
    # sits above it and is withheld.
    assert records == []
    assert control["count"] == 0


async def test_invalid_cursor_422(ctx):
    c, factory = ctx
    resp = await c.get("/api/articles/delta?since=not-a-cursor!!")
    assert resp.status_code == 422
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_delta_feed.py -v`
Expected: FAIL — route not defined (404 / 422 mismatches).

- [ ] **Step 3: Add the route**

In `app/routes/articles.py`, add imports near the top:

```python
from fastapi.responses import StreamingResponse

from app.schemas.delta import decode_delta_cursor
from app.services import delta_feed
```

Insert this route **immediately after `list_articles` and before `get_article`** (so the literal `/delta` path is registered before `/{article_id}`):

```python
@router.get("/delta")
async def article_delta_feed(
    since: str | None = Query(
        None,
        description="Opaque cursor from a prior pull's control record. "
        "Omit for a full bootstrap snapshot.",
    ),
    source_id: uuid.UUID | None = Query(None),
    vendor_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """Stream article changes as JSONL for the downstream GraphRAG pipeline.

    - ``since`` omitted → full bootstrap snapshot (every current, visible,
      non-removed article as ``added``); the control record's ``next_since`` is
      the watermark to continue from.
    - ``since`` present → changes after that watermark (added/updated content
      records + removed tombstones), gap-free under concurrent runs.

    The final line is always a control record: ``{"control":"cursor",
    "next_since": "...","count": N}``. A truncated stream lacks it — clients must
    only advance their stored cursor on a clean finish.
    """
    visible = principal.visible_vendor_ids()

    if since is None:
        gen = delta_feed.stream_bootstrap(
            db, source_id=source_id, vendor_id=vendor_id, visible_vendor_ids=visible,
        )
    else:
        try:
            since_seq = decode_delta_cursor(since)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        gen = delta_feed.stream_delta(
            db, since_seq=since_seq, source_id=source_id, vendor_id=vendor_id,
            visible_vendor_ids=visible,
        )

    return StreamingResponse(gen, media_type="application/x-ndjson")
```

> Streaming from the injected `db` session is safe here: on FastAPI 0.137.x the
> yield-dependency teardown is bound to the response lifecycle, so `get_db`'s
> session stays open until the `StreamingResponse` body is fully consumed. Do not
> close or reuse `db` elsewhere in this handler.

> Note: when auth is disabled (no `DOCEXTRACTOR_AUTH_JWT_SECRET`), `visible_vendor_ids()` returns `None` (all visible), so the test fixture — which sets no secret — streams everything. An empty `visible` list (a user with zero vendor grants) yields an empty stream plus the control record, which the generators already produce naturally.

- [ ] **Step 4: Run the integration tests to verify they pass**

Run: `pytest tests/test_delta_feed.py -v`
Expected: 5 passed.

- [ ] **Step 5: Verify the route ordering did not shadow `get_article`**

Run: `pytest tests/test_enhanced_search.py -v`
Expected: still passes (the `/{article_id}` route is unaffected; `delta` never parses as a UUID).

- [ ] **Step 6: Commit**

```bash
git add backend/app/routes/articles.py backend/tests/test_delta_feed.py
git commit -m "feat(delta): GET /api/articles/delta streaming JSONL feed"
```

---

## Task 7: Enrich the `extraction_complete` webhook with a delta summary

**Files:**
- Modify: `app/services/firecrawl.py` (the `extraction_complete` dispatch block, ~lines 2126–2139)
- Test: `tests/test_delta_webhook.py`

**Interfaces:**
- Consumes: `change_log.run_change_counts` (Task 2); `encode_delta_cursor` (Task 4).
- Produces: the `extraction_complete` webhook payload's `extra` now carries
  `"delta": {"added": int, "updated": int, "removed": int, "watermark": str}`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_delta_webhook.py`:

```python
"""The extraction_complete webhook payload carries a delta summary block."""
import os
import sys
import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core.database import Base
from app.models import Vendor, Product, DocumentationSource, ExtractionRun, Article
from app.models.content_change import ContentChange
from app.services import change_log
from app.schemas.delta import decode_delta_cursor

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with factory() as s:
        yield s
    await engine.dispose()


async def test_run_change_counts_feeds_delta_block(session):
    v = Vendor(name="V"); session.add(v); await session.flush()
    p = Product(name="P", vendor_id=v.id); session.add(p); await session.flush()
    src = DocumentationSource(name="S", base_url="https://x", product_id=p.id)
    session.add(src); await session.flush()
    run = ExtractionRun(source_id=src.id); session.add(run); await session.flush()
    for i in range(2):
        session.add(ContentChange(source_id=src.id, run_id=run.id, change_type="added", topic_key=f"t{i}"))
    session.add(ContentChange(source_id=src.id, run_id=run.id, change_type="updated", topic_key="u"))
    await session.commit()

    counts = await change_log.run_change_counts(session, run.id)
    assert counts == {"added": 2, "updated": 1, "removed": 0}
```

> This test locks the counts helper that feeds the webhook block. The dispatch wiring itself is exercised by `tests/test_webhooks.py` after this change (the payload gains a `delta` key); update that suite if it asserts an exact `extra` shape.

- [ ] **Step 2: Run to verify it passes for the helper (already implemented in Task 2)**

Run: `pytest tests/test_delta_webhook.py -v`
Expected: PASS (the helper exists; this pins its contract before the wiring change).

- [ ] **Step 3: Wire the delta block into the webhook payload**

In `app/services/firecrawl.py`, add the import near the other schema imports at the top:

```python
from app.schemas.delta import encode_delta_cursor
```

Replace the `extraction_complete` dispatch block (currently lines ~2127–2139) with a version that computes and attaches the delta summary. The counts query and the max-seq watermark both run on the live `db` before the fire-and-forget spawn:

```python
            # Fire extraction_complete webhook (best-effort, tracked fire-and-forget).
            if webhook_dispatcher.run_has_subscribers(run_pk, "extraction_complete"):
                delta_counts = await change_log.run_change_counts(db, run_pk)
                max_seq = (
                    await db.execute(select(func.max(ContentChange.id)))
                ).scalar() or 0
                webhook_dispatcher.spawn_event(
                    event_type="extraction_complete",
                    run_id=run_pk,
                    source_id=source_id,
                    extra={
                        "status": "completed",
                        "articles_extracted": int(extracted or 0),
                        "articles_updated": int(updated or 0),
                        "articles_unchanged": int(unchanged or 0),
                        "articles_resumed": int(resumed or 0),
                        "delta": {
                            "added": delta_counts["added"],
                            "updated": delta_counts["updated"],
                            "removed": delta_counts["removed"],
                            "watermark": encode_delta_cursor(max_seq),
                        },
                    },
                )
            webhook_dispatcher.finish_run(run_pk)
```

Add the required imports at the top of `firecrawl.py` if not already present: `from app.models.content_change import ContentChange` and ensure `func` and `select` are imported (they already are — the module uses `select`/`update`; add `func` to the existing `from sqlalchemy import ...` line if missing).

- [ ] **Step 4: Run the webhook tests**

Run: `pytest tests/test_delta_webhook.py tests/test_webhooks.py -v`
Expected: pass. If a `test_webhooks.py` assertion pins the exact `extra` dict for `extraction_complete`, extend it to expect the additional `delta` key.

- [ ] **Step 5: Full backend test run**

Run: `cd backend && pytest -q`
Expected: green (no regressions).

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/firecrawl.py backend/tests/test_delta_webhook.py
git commit -m "feat(delta): extraction_complete webhook carries delta summary + watermark"
```

---

## Task 8: Close the multi-replica commit-order gap (run_start floor) + provenance/bootstrap fixes

Added after the final whole-branch review. The safe-ceiling in Task 5 keys on *committed* active-run rows, but a run's **first** outbox row is assigned an id at flush and is invisible until commit; in that window a concurrent run's higher id can be served and the cursor advanced past the uncommitted lower id (a silently dropped change). Fix: every run commits a `run_start` sentinel row into `content_changes` **before** processing any article, giving each active run a committed floor in `content_changes.id` space from the moment it is active. `_safe_ceiling` already counts active-run rows, so the sentinel is picked up automatically; the stream skips sentinel rows. Also folds in two review fixes: the delta content record's `run_id` should be the change's run (not the article's latest), and `stream_bootstrap`'s watermark must respect the ceiling.

**Files:**
- Modify: `app/models/content_change.py` (add `RUN_START` enum value)
- Modify: `app/services/change_log.py` (add `record_run_start`)
- Modify: `app/services/firecrawl.py` (commit a run_start row at the top of `extract_source`)
- Modify: `app/services/delta_feed.py` (skip sentinels explicitly; content record uses change's run_id; bootstrap watermark respects ceiling; docstring)
- Modify: `docs/superpowers/specs/2026-07-10-graphrag-delta-feed-design.md` (document the sentinel floor)
- Test: `tests/test_delta_feed.py` (append two tests)

**Interfaces:**
- Produces: `ChangeType.RUN_START = "run_start"`; `change_log.record_run_start(db, *, source_id, run_id)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_delta_feed.py`:

```python
async def test_run_start_floor_withholds_higher_rows_of_other_runs(ctx):
    # A run_start sentinel for an ACTIVE run establishes a committed floor. A
    # COMPLETED run's change with a HIGHER id must be withheld until the active
    # run finishes — this is the mechanism that closes the flush→commit window.
    c, factory = ctx
    _, src_id, active_run = await _seed_source(factory, run_status=RunStatus.RUNNING)
    async with factory() as s:
        s.add(ContentChange(article_id=None, source_id=src_id, run_id=active_run,
                            change_type="run_start", topic_key=None))
        await s.commit()
    # Completed run whose article change has a higher id than the sentinel.
    async with factory() as s:
        done = ExtractionRun(source_id=src_id, status=RunStatus.COMPLETED)
        s.add(done); await s.flush()
        done_id = done.id
        await s.commit()
    await _add_article_change(factory, src_id, done_id, url="https://x/done", title="Done")

    r1 = await c.get(f"/api/articles/delta?since={encode_delta_cursor(0)}")
    recs1, ctrl1 = _parse(r1.text)
    assert recs1 == [] and ctrl1["count"] == 0  # withheld: sentinel floor blocks higher ids

    # Complete the active run → floor lifts → the completed row is served, and the
    # run_start sentinel is skipped (not emitted as a record).
    async with factory() as s:
        run = await s.get(ExtractionRun, active_run)
        run.status = RunStatus.COMPLETED
        await s.commit()
    r2 = await c.get(f"/api/articles/delta?since={encode_delta_cursor(0)}")
    recs2, _ = _parse(r2.text)
    titles = [r.get("title") for r in recs2]
    assert titles == ["Done"]  # exactly the real change; sentinel skipped, no gap


async def test_content_record_run_id_is_change_run_not_article_run(ctx):
    # The delta content record's run_id must be the CHANGE's run, not the article's
    # latest extraction_run_id (they diverge if a later run touched the article).
    c, factory = ctx
    _, src_id, run_a = await _seed_source(factory)
    async with factory() as s:
        run_b = ExtractionRun(source_id=src_id, status=RunStatus.COMPLETED)
        s.add(run_b); await s.flush()
        run_b_id = run_b.id
        art = Article(
            source_id=src_id, extraction_run_id=run_b_id, created_run_id=run_a,
            title="A", source_url="https://x/a", topic_key="https://x/a",
            content_markdown="# A", content_hash="h",
        )
        s.add(art); await s.flush()
        # The change belongs to run_a, but the article's latest run is run_b.
        s.add(ContentChange(article_id=art.id, source_id=src_id, run_id=run_a,
                            change_type="updated", content_hash="h", topic_key="https://x/a"))
        await s.commit()

    resp = await c.get(f"/api/articles/delta?since={encode_delta_cursor(0)}")
    recs, _ = _parse(resp.text)
    assert len(recs) == 1
    assert recs[0]["run_id"] == str(run_a)      # the change's run
    assert recs[0]["run_id"] != str(run_b_id)   # not the article's latest run
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/test_delta_feed.py::test_run_start_floor_withholds_higher_rows_of_other_runs tests/test_delta_feed.py::test_content_record_run_id_is_change_run_not_article_run -v`
Expected: FAIL — `test_content_record_...` fails because the record currently uses `article.extraction_run_id` (returns run_b). (`test_run_start_...` may already pass if the stream happens to skip the sentinel — that's fine; it locks the behavior.)

- [ ] **Step 3: Add the `RUN_START` enum value**

In `app/models/content_change.py`, add to `ChangeType`:

```python
class ChangeType(str, Enum):
    ADDED = "added"
    UPDATED = "updated"
    REMOVED = "removed"
    RUN_START = "run_start"
```

No migration needed — `change_type` is `String(16)`, not a DB enum, and `"run_start"` fits.

- [ ] **Step 4: Add the `record_run_start` helper**

In `app/services/change_log.py`, add:

```python
async def record_run_start(
    db: AsyncSession, *, source_id: uuid.UUID, run_id: uuid.UUID
) -> None:
    """Append a ``run_start`` sentinel outbox row for a run (caller commits).

    Committed before the run processes any article, this gives the run a visible
    floor in content_changes.id space so the delta feed's safe-ceiling withholds
    the run's later (possibly mid-commit) rows until it finishes — closing the
    flush→commit gap under concurrent multi-replica runs. The feed skips these
    rows (they carry no article and are not a change type it emits)."""
    db.add(
        ContentChange(
            article_id=None,
            source_id=source_id,
            run_id=run_id,
            change_type=ChangeType.RUN_START.value,
            content_hash=None,
            topic_key=None,
        )
    )
```

- [ ] **Step 5: Commit a run_start row at the top of `extract_source`**

In `app/services/firecrawl.py`, immediately after `run_pk = run.id` (currently line 1749, inside `extract_source`, before the auth-resolution block and the PDF/web branch), insert:

```python
        # Commit a run_start sentinel into the outbox before any article work, so
        # this run has a COMMITTED floor in content_changes.id space from the moment
        # it is active. The delta feed's safe-ceiling keys on active-run rows; the
        # committed floor closes the flush→commit window where a run's first real
        # row is assigned an id but not yet visible (which could otherwise let a
        # concurrent run's higher-id row be served and advance the cursor past the
        # uncommitted lower id). See services/delta_feed.py. Harmless on resume
        # (an extra, higher-id floor that doesn't lower the run's true minimum).
        await change_log.record_run_start(db, source_id=source_id, run_id=run_pk)
        await db.commit()
```

(`change_log` is already imported in `firecrawl.py` from Task 3.)

- [ ] **Step 6: Update `delta_feed.py` — skip sentinels, change-run_id, bootstrap ceiling, docstring**

6a. In `_content_record`, add a `run_id` parameter and use it instead of `article.extraction_run_id`:

```python
async def _content_record(db, resolver, *, seq, change_type, article, vendor_name, product_name, run_id):
```
and change the run_id field to:
```python
        "run_id": str(run_id) if run_id else None,
```

6b. In `stream_delta`, make the emit branch explicit and pass the change's run_id. Replace the per-row mapping block:

```python
        for change, article, vendor_name, product_name in rows:
            last = change.id
            if change.change_type == "removed":
                yield _line(_tombstone_record(change))
                count += 1
            elif change.change_type in ("added", "updated") and article is not None:
                yield _line(await _content_record(
                    db, resolver, seq=change.id, change_type=change.change_type,
                    article=article, vendor_name=vendor_name, product_name=product_name,
                    run_id=change.run_id,
                ))
                count += 1
            # else: run_start sentinel, or added/updated whose article was
            # hard-deleted → skip (advancing `last` past it is safe: it is
            # permanent — below the ceiling, from a terminal run).
```

6c. In `stream_bootstrap`, pass the article's own run and make the bootstrap watermark respect the ceiling. Change the `_content_record` call to add `run_id=article.extraction_run_id`, and change the `max_seq` computation to:

```python
    # Watermark for the follow-up delta = current global max outbox id, but never
    # past the safe ceiling — otherwise a change committed below max_seq by a run
    # still active during bootstrap would be skipped by the first delta pull.
    max_seq = (await db.execute(select(func.max(ContentChange.id)))).scalar() or 0
    ceiling = await _safe_ceiling(db)
    if ceiling is not None:
        max_seq = min(max_seq, ceiling - 1)
```

(The final `yield _line({"control": "cursor", "next_since": encode_delta_cursor(max_seq), ...})` line is unchanged; it already encodes `max_seq`.)

6d. In the module docstring, replace the paragraph that begins `Ordering is by content_changes.id alone.` through the end of that paragraph with:

```python
"""Streaming JSONL delta feed over the content_changes outbox.

Ordering is by content_changes.id alone. Gap-freeness under concurrent runs is
guaranteed by a "safe ceiling": the lowest id belonging to any still-active run.
Each run commits a ``run_start`` sentinel row before processing any article, so
an active run always has a COMMITTED floor in id space from the moment it is
active — this closes the flush→commit window where a run's first real row is
assigned an id but not yet visible. Because an active run's rows (starting at its
sentinel) all have id >= its floor, every row served with id < ceiling provably
belongs to a terminal run, so serving strictly below the ceiling never skips a
slow or just-started run's not-yet-committed higher ids. Sentinel rows carry no
article and are skipped by the stream.
"""
```

- [ ] **Step 7: Run the new + existing delta tests**

Run: `pytest tests/test_delta_feed.py -v`
Expected: all pass (the 5 originals + 2 new). Output pristine aside from the known global SWIG import warnings.

- [ ] **Step 8: Regression**

Run: `pytest tests/test_change_log.py tests/test_change_log_wiring.py tests/test_delta_webhook.py tests/test_incremental.py tests/test_integration.py tests/test_reconcile_removals.py -v`
Expected: all pass. (`run_change_counts` ignores the `run_start` type — it only reads added/updated/removed — so webhook counts are unaffected. The wiring tests call `process_article_result`/`_reconcile_removals` directly, not `extract_source`, so no sentinel is added there.)

- [ ] **Step 9: Update the spec's gap-free section**

In `docs/superpowers/specs/2026-07-10-graphrag-delta-feed-design.md`, in the "Why the watermark is gap-free under concurrent runs" section, append a paragraph:

```markdown
**Run-start floor (commit-order safety).** A `BIGSERIAL` id is assigned at insert
but a row is invisible to other sessions until its transaction commits, so a run's
first outbox row has a brief flush→commit window where its low id is unseen. To
keep the safe-ceiling correct across that window, every run commits a `run_start`
sentinel row into `content_changes` before it processes any article. That gives
each active run a committed floor in id space from the moment it is active, so the
ceiling always reflects it and the run's later (possibly mid-commit) rows are
withheld until the run reaches a terminal state. The feed skips sentinel rows.
```

- [ ] **Step 10: Commit**

```bash
git add backend/app/models/content_change.py backend/app/services/change_log.py \
        backend/app/services/firecrawl.py backend/app/services/delta_feed.py \
        backend/tests/test_delta_feed.py docs/superpowers/specs/2026-07-10-graphrag-delta-feed-design.md
git commit -m "fix(delta): run_start floor closes multi-replica commit-order gap; change-run_id + bootstrap ceiling"
```

---

## Self-Review

**Spec coverage:**

- Outbox table `content_changes` with BIGSERIAL watermark → Task 1. ✅
- Rows written transactionally on add/update/remove → Task 3 (+ helpers Task 2). ✅
- Safe-ceiling gap-free ordering under concurrent runs → Task 5 `_safe_ceiling` + Task 6 `test_safe_ceiling_withholds_active_run_rows`. ✅
- `GET /api/articles/delta`, JSONL streaming, RBAC vendor filter, `source_id`/`vendor_id` sharding → Task 6. ✅
- Bootstrap snapshot when `since` omitted; `next_since` = current max seq → Task 5 `stream_bootstrap` + Task 6 `test_bootstrap_streams_all_articles`. ✅
- Tombstone records for removals → Task 5 `_tombstone_record` + Task 6 `test_removed_emits_tombstone`. ✅
- Trailing control record carrying `next_since` → Task 5 (both generators). ✅
- Opaque versioned cursor, 422 on malformed → Task 4 + Task 6 `test_invalid_cursor_422`. ✅
- `extraction_complete` webhook `delta` block (counts + watermark) → Task 7. ✅
- Provenance fields (vendor/product/parent/top-level chapter) derived from live TOC → Task 5 `ChapterResolver` + `_content_record`. ✅
- `images[].description`/`kind` present as `null` until Spec 2 → Task 5 `_images_payload` uses `getattr(..., None)`. ✅

**Placeholder scan:** No TBD/TODO; every code step shows complete code; every test step shows the assertion. ✅

**Type consistency:** `record_change`/`record_removals`/`run_change_counts` signatures identical across Tasks 2, 3, 7. `encode_delta_cursor`/`decode_delta_cursor` identical across Tasks 4, 5, 6, 7. `stream_delta`/`stream_bootstrap` keyword params (`since_seq`, `source_id`, `vendor_id`, `visible_vendor_ids`) match between Task 5 definition and Task 6 call sites. Record field names match the schema documented in Task 4. ✅

**Note for the implementer:** `content_hash` is computed on the pre-image-rewrite "canonical" markdown (`firecrawl.py:779`), a deliberate existing invariant. This plan does not touch hashing; Spec 2 (VLM image descriptions) will need to inject captions into that canonical markdown before the hash is taken. Out of scope here.
