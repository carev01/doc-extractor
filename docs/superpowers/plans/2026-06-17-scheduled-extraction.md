# Scheduled Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add unattended, recurring extraction by decomposing the app into web/worker/scheduler processes coordinated through a Postgres-backed job queue (the `extraction_runs` table), with a per-source schedule managed from the UI.

**Architecture:** The web process enqueues runs (manual or via the scheduler) but no longer executes them. Worker processes claim `pending` runs with `FOR UPDATE SKIP LOCKED` and run the existing `extract_source` engine. A single scheduler process ticks every ~30s to enqueue due schedules (coalescing when a source already has an active run) and reap runs whose worker died. All three are the same image, selected by command.

**Tech Stack:** FastAPI, SQLAlchemy async (asyncpg), PostgreSQL, Alembic, Pydantic v2, `croniter` (new), stdlib `zoneinfo`; React 19 + TypeScript + Vite frontend.

## Global Constraints

- Python enum labels are stored in Postgres as the member **NAME in uppercase** (`'RUNNING'`, `'PENDING'`), per the initial migration — every SQL predicate and `ALTER TYPE ADD VALUE` must use uppercase labels.
- All new models must be imported in `app/models/__init__.py` so `Base.metadata` is populated before `create_all` (startup invariant).
- Always pass `run_id` when calling `firecrawl_service.extract_source` (no duplicate run rows).
- Settings use the `DOCEXTRACTOR_` env prefix.
- Backend tests use the async fixture from `tests/test_versions.py`: a per-test `create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)`, `Base.metadata.drop_all`/`create_all`, `get_db` overridden, `httpx.AsyncClient` with `ASGITransport`. `TEST_DATABASE_URL` = main URL with `/docextractor_test`. Run from `backend/` with `pytest`.
- Concurrency-defining objects (`ix_runs_pending`, `uq_active_run_per_source`) MUST be declared in the model's `__table_args__` so `create_all` (and therefore tests) include them, AND mirrored in the Alembic migration for production.
- Frontend is verified via `npm run build` + `npm run lint` (no component unit-test runner in this repo).
- Deploy is rebuild-to-test under docker-compose (code is baked into images).

---

### Task 1: Queue columns, RunStatus enum, Schedule model, migration

**Files:**
- Modify: `backend/app/models/extraction_run.py`
- Create: `backend/app/models/schedule.py`
- Modify: `backend/app/models/__init__.py`
- Create: `backend/alembic/versions/c2d3e4f5a6b7_add_scheduling_and_queue.py`
- Test: `backend/tests/test_queue.py` (created here, used in Task 3)

**Interfaces:**
- Produces: `RunStatus.PENDING = "pending"`, `RunStatus.CANCELLED = "cancelled"`; `ExtractionRun` columns `trigger: str`, `claimed_by: str|None`, `claimed_at: datetime|None`, `heartbeat_at: datetime|None`, `attempts: int`; partial indexes `ix_runs_pending`, `uq_active_run_per_source`.
- Produces: `Schedule` model with `source_id` (unique), `enabled`, `frequency`, `time_of_day`, `day_of_week`, `day_of_month`, `cron`, `timezone`, `next_run_at`, `last_run_at`, `last_run_id`.

- [ ] **Step 1: Add enum members + queue columns + indexes to `ExtractionRun`**

In `backend/app/models/extraction_run.py`, update imports and the `RunStatus` enum, and add columns + `__table_args__`:

```python
import uuid
from datetime import datetime
from enum import Enum

from sqlalchemy import (
    DateTime, Enum as SAEnum, ForeignKey, Index, Integer, String, func, text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ExtractionRun(Base):
    __tablename__ = "extraction_runs"

    __table_args__ = (
        Index(
            "ix_runs_pending", "created_at",
            postgresql_where=text("status = 'PENDING'"),
        ),
        Index(
            "uq_active_run_per_source", "source_id", unique=True,
            postgresql_where=text("status IN ('PENDING', 'RUNNING')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documentation_sources.id", ondelete="CASCADE"),
        nullable=False,
    )

    status: Mapped[RunStatus] = mapped_column(
        SAEnum(RunStatus), default=RunStatus.RUNNING, nullable=False
    )
    # Queue / worker-coordination columns.
    trigger: Mapped[str] = mapped_column(String(16), default="manual", nullable=False)
    claimed_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    articles_extracted: Mapped[int] = mapped_column(Integer, default=0)
    articles_total: Mapped[int] = mapped_column(Integer, default=0)
    articles_unchanged: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    articles_updated: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    error_message: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    firecrawl_job_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    current_phase: Mapped[str | None] = mapped_column(String(64), nullable=True)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    source: Mapped["DocumentationSource"] = relationship(
        "DocumentationSource", back_populates="extraction_runs"
    )
    articles: Mapped[list["Article"]] = relationship(
        "Article", back_populates="extraction_run"
    )
```

- [ ] **Step 2: Create the `Schedule` model**

Create `backend/app/models/schedule.py`:

```python
"""Schedule model — recurring extraction config for a source."""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean, DateTime, ForeignKey, Integer, String, func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class Schedule(Base):
    __tablename__ = "schedules"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documentation_sources.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Friendly fields (persisted so the UI can reconstruct the selection)...
    frequency: Mapped[str] = mapped_column(String(16), nullable=False)  # hourly|daily|weekly|monthly
    time_of_day: Mapped[str] = mapped_column(String(5), default="02:00", nullable=False)  # HH:MM
    day_of_week: Mapped[int | None] = mapped_column(Integer, nullable=True)   # 0-6, 0=Sun (weekly)
    day_of_month: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 1-28 (monthly)

    # ...and the canonical cron form the engine actually evaluates.
    cron: Mapped[str] = mapped_column(String(128), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", nullable=False)

    next_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("extraction_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    source: Mapped["DocumentationSource"] = relationship("DocumentationSource")
```

- [ ] **Step 3: Register `Schedule` in `app/models/__init__.py`**

Add the import and `__all__` entry:

```python
from app.models.schedule import Schedule
```
and add `"Schedule",` to `__all__`.

- [ ] **Step 4: Write the migration**

Create `backend/alembic/versions/c2d3e4f5a6b7_add_scheduling_and_queue.py`:

```python
"""add scheduling and queue

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "c2d3e4f5a6b7"
down_revision: Union[str, Sequence[str], None] = "b1c2d3e4f5a6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # New enum labels must be committed before they can be referenced.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE runstatus ADD VALUE IF NOT EXISTS 'PENDING'")
        op.execute("ALTER TYPE runstatus ADD VALUE IF NOT EXISTS 'CANCELLED'")

    op.add_column("extraction_runs", sa.Column("trigger", sa.String(16), server_default="manual", nullable=False))
    op.add_column("extraction_runs", sa.Column("claimed_by", sa.String(255), nullable=True))
    op.add_column("extraction_runs", sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("extraction_runs", sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("extraction_runs", sa.Column("attempts", sa.Integer(), server_default="0", nullable=False))

    op.create_index(
        "ix_runs_pending", "extraction_runs", ["created_at"],
        postgresql_where=sa.text("status = 'PENDING'"),
    )
    op.create_index(
        "uq_active_run_per_source", "extraction_runs", ["source_id"], unique=True,
        postgresql_where=sa.text("status IN ('PENDING', 'RUNNING')"),
    )

    op.create_table(
        "schedules",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("source_id", UUID(as_uuid=True), sa.ForeignKey("documentation_sources.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("frequency", sa.String(16), nullable=False),
        sa.Column("time_of_day", sa.String(5), nullable=False, server_default="02:00"),
        sa.Column("day_of_week", sa.Integer(), nullable=True),
        sa.Column("day_of_month", sa.Integer(), nullable=True),
        sa.Column("cron", sa.String(128), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="UTC"),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_id", UUID(as_uuid=True), sa.ForeignKey("extraction_runs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("schedules")
    op.drop_index("uq_active_run_per_source", table_name="extraction_runs")
    op.drop_index("ix_runs_pending", table_name="extraction_runs")
    op.drop_column("extraction_runs", "attempts")
    op.drop_column("extraction_runs", "heartbeat_at")
    op.drop_column("extraction_runs", "claimed_at")
    op.drop_column("extraction_runs", "claimed_by")
    op.drop_column("extraction_runs", "trigger")
    # Enum labels are left in place (Postgres cannot drop enum values cleanly).
```

- [ ] **Step 5: Apply the migration and verify the schema**

Run: `cd backend && alembic upgrade head`
Expected: completes without error; `alembic current` shows `c2d3e4f5a6b7`.

- [ ] **Step 6: Commit**

```bash
git add backend/app/models/extraction_run.py backend/app/models/schedule.py backend/app/models/__init__.py backend/alembic/versions/c2d3e4f5a6b7_add_scheduling_and_queue.py
git commit -m "feat(db): add scheduling table and extraction-run queue columns"
```

---

### Task 2: Cron service (pure functions)

**Files:**
- Create: `backend/app/services/cron.py`
- Test: `backend/tests/test_cron.py`
- Modify: `backend/requirements.txt`

**Interfaces:**
- Produces: `build_cron(frequency: str, time_of_day: str = "02:00", day_of_week: int | None = None, day_of_month: int | None = None) -> str`
- Produces: `compute_next_run(cron: str, timezone: str, after: datetime) -> datetime` (returns a UTC tz-aware datetime)

- [ ] **Step 1: Add `croniter` to requirements**

Append to `backend/requirements.txt`:
```
croniter==3.0.3
```
Run: `cd backend && pip install croniter==3.0.3`
Expected: installs successfully.

- [ ] **Step 2: Write the failing test**

Create `backend/tests/test_cron.py`:

```python
import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.cron import build_cron, compute_next_run


def test_build_cron_daily():
    assert build_cron("daily", "02:30") == "30 2 * * *"


def test_build_cron_hourly_uses_minute():
    assert build_cron("hourly", "00:15") == "15 * * * *"


def test_build_cron_weekly_includes_dow():
    assert build_cron("weekly", "02:00", day_of_week=0) == "0 2 * * 0"


def test_build_cron_monthly_includes_dom():
    assert build_cron("monthly", "02:00", day_of_month=1) == "0 2 1 * *"


def test_build_cron_rejects_unknown_frequency():
    with pytest.raises(ValueError):
        build_cron("yearly", "02:00")


def test_compute_next_run_is_utc_and_in_future():
    after = datetime(2026, 6, 17, 0, 0, tzinfo=timezone.utc)
    nxt = compute_next_run("0 2 * * *", "UTC", after)
    assert nxt == datetime(2026, 6, 17, 2, 0, tzinfo=timezone.utc)


def test_compute_next_run_respects_timezone():
    # Lisbon is UTC+1 in June (DST); 02:00 local == 01:00 UTC.
    after = datetime(2026, 6, 17, 0, 0, tzinfo=timezone.utc)
    nxt = compute_next_run("0 2 * * *", "Europe/Lisbon", after)
    assert nxt == datetime(2026, 6, 17, 1, 0, tzinfo=timezone.utc)


def test_compute_next_run_catches_up_once_when_overdue():
    # 'after' is past today's 02:00 fire -> next fire is tomorrow, computed once.
    after = datetime(2026, 6, 17, 4, 30, tzinfo=timezone.utc)
    nxt = compute_next_run("0 2 * * *", "UTC", after)
    assert nxt == datetime(2026, 6, 18, 2, 0, tzinfo=timezone.utc)
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd backend && pytest tests/test_cron.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.cron'`.

- [ ] **Step 4: Write the implementation**

Create `backend/app/services/cron.py`:

```python
"""Friendly-preset -> cron construction and next-run computation."""

from datetime import datetime
from zoneinfo import ZoneInfo

from croniter import croniter

VALID_FREQUENCIES = {"hourly", "daily", "weekly", "monthly"}
_UTC = ZoneInfo("UTC")


def _parse_hh_mm(time_of_day: str) -> tuple[int, int]:
    hour_s, minute_s = time_of_day.split(":")
    return int(hour_s), int(minute_s)


def build_cron(
    frequency: str,
    time_of_day: str = "02:00",
    day_of_week: int | None = None,
    day_of_month: int | None = None,
) -> str:
    """Build a 5-field cron string from friendly preset fields."""
    if frequency not in VALID_FREQUENCIES:
        raise ValueError(f"Unknown frequency: {frequency}")
    hour, minute = _parse_hh_mm(time_of_day)
    if frequency == "hourly":
        return f"{minute} * * * *"
    if frequency == "daily":
        return f"{minute} {hour} * * *"
    if frequency == "weekly":
        dow = 0 if day_of_week is None else day_of_week
        return f"{minute} {hour} * * {dow}"
    # monthly
    dom = 1 if day_of_month is None else day_of_month
    return f"{minute} {hour} {dom} * *"


def compute_next_run(cron: str, timezone: str, after: datetime) -> datetime:
    """Return the next fire time strictly after `after`, as a UTC datetime."""
    tz = ZoneInfo(timezone)
    base = after.astimezone(tz)
    nxt = croniter(cron, base).get_next(datetime)
    return nxt.astimezone(_UTC)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend && pytest tests/test_cron.py -v`
Expected: all 8 PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/cron.py backend/tests/test_cron.py backend/requirements.txt
git commit -m "feat(scheduler): add cron construction and next-run computation"
```

---

### Task 3: Queue service (enqueue / claim / reap)

**Files:**
- Create: `backend/app/services/queue.py`
- Test: `backend/tests/test_queue.py`

**Interfaces:**
- Consumes: `ExtractionRun`, `RunStatus` (Task 1).
- Produces: `class ActiveRunExists(Exception)`; `async enqueue_run(db, source_id, trigger="manual") -> ExtractionRun`; `async claim_next_run(db, worker_id) -> ExtractionRun | None`; `async reap_stale_runs(db, max_attempts=3, stale_seconds=300) -> int`.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_queue.py`:

```python
import asyncio
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core.database import Base
from app.models import Vendor, DocumentationSource, ExtractionRun
from app.models.extraction_run import RunStatus
from app.services.queue import (
    ActiveRunExists, enqueue_run, claim_next_run, reap_stale_runs,
)

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"


@pytest_asyncio.fixture
async def sessions():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield factory
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def _make_source(db) -> uuid.UUID:
    vendor = Vendor(name="V")
    db.add(vendor)
    await db.flush()
    src = DocumentationSource(vendor_id=vendor.id, name="S", base_url="http://x")
    db.add(src)
    await db.commit()
    await db.refresh(src)
    return src.id


@pytest.mark.asyncio
async def test_enqueue_creates_pending_run(sessions):
    async with sessions() as db:
        source_id = await _make_source(db)
        run = await enqueue_run(db, source_id, trigger="scheduled")
        assert run.status == RunStatus.PENDING
        assert run.trigger == "scheduled"


@pytest.mark.asyncio
async def test_enqueue_second_active_run_raises(sessions):
    async with sessions() as db:
        source_id = await _make_source(db)
        await enqueue_run(db, source_id)
        with pytest.raises(ActiveRunExists):
            await enqueue_run(db, source_id)


@pytest.mark.asyncio
async def test_claim_marks_running_and_increments_attempts(sessions):
    async with sessions() as db:
        source_id = await _make_source(db)
        await enqueue_run(db, source_id)
    async with sessions() as db:
        run = await claim_next_run(db, "worker-1")
        assert run is not None
        assert run.status == RunStatus.RUNNING
        assert run.claimed_by == "worker-1"
        assert run.attempts == 1
        assert run.heartbeat_at is not None
    async with sessions() as db:
        assert await claim_next_run(db, "worker-2") is None


@pytest.mark.asyncio
async def test_concurrent_claims_never_grab_same_row(sessions):
    # Two pending runs (different sources), two workers, claimed concurrently.
    async with sessions() as db:
        s1 = await _make_source(db)
    async with sessions() as db:
        s2 = await _make_source(db)
    async with sessions() as db:
        await enqueue_run(db, s1)
    async with sessions() as db:
        await enqueue_run(db, s2)

    async def claim(name):
        async with sessions() as db:
            return await claim_next_run(db, name)

    a, b = await asyncio.gather(claim("w1"), claim("w2"))
    ids = {r.id for r in (a, b) if r is not None}
    assert len(ids) == 2  # distinct rows, none grabbed twice


@pytest.mark.asyncio
async def test_reap_requeues_stale_then_fails_at_cap(sessions):
    async with sessions() as db:
        source_id = await _make_source(db)
        stale = ExtractionRun(
            source_id=source_id, status=RunStatus.RUNNING, attempts=1,
            heartbeat_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        )
        db.add(stale)
        await db.commit()
        run_id = stale.id
    async with sessions() as db:
        n = await reap_stale_runs(db, max_attempts=3, stale_seconds=300)
        assert n == 1
    async with sessions() as db:
        run = (await db.execute(select(ExtractionRun).where(ExtractionRun.id == run_id))).scalar_one()
        assert run.status == RunStatus.PENDING  # attempts(1) < cap -> requeued
        assert run.claimed_by is None

    # Bump attempts to the cap and reap again -> failed.
    async with sessions() as db:
        run = (await db.execute(select(ExtractionRun).where(ExtractionRun.id == run_id))).scalar_one()
        run.status = RunStatus.RUNNING
        run.attempts = 3
        run.heartbeat_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        await db.commit()
    async with sessions() as db:
        await reap_stale_runs(db, max_attempts=3, stale_seconds=300)
    async with sessions() as db:
        run = (await db.execute(select(ExtractionRun).where(ExtractionRun.id == run_id))).scalar_one()
        assert run.status == RunStatus.FAILED
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && pytest tests/test_queue.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.queue'`.

- [ ] **Step 3: Write the implementation**

Create `backend/app/services/queue.py`:

```python
"""Postgres-backed extraction job queue (the extraction_runs table)."""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.extraction_run import ExtractionRun, RunStatus


class ActiveRunExists(Exception):
    """Raised when a source already has a pending/running run (coalesce/409)."""


async def enqueue_run(
    db: AsyncSession, source_id: uuid.UUID, trigger: str = "manual"
) -> ExtractionRun:
    """Insert a pending run. Raises ActiveRunExists if one is already active."""
    run = ExtractionRun(
        source_id=source_id, status=RunStatus.PENDING, trigger=trigger
    )
    db.add(run)
    try:
        await db.commit()
    except IntegrityError as exc:  # uq_active_run_per_source
        await db.rollback()
        raise ActiveRunExists(str(source_id)) from exc
    await db.refresh(run)
    return run


async def claim_next_run(
    db: AsyncSession, worker_id: str
) -> ExtractionRun | None:
    """Atomically claim the oldest pending run, or None if the queue is empty."""
    result = await db.execute(
        select(ExtractionRun)
        .where(ExtractionRun.status == RunStatus.PENDING)
        .order_by(ExtractionRun.created_at)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    run = result.scalar_one_or_none()
    if run is None:
        return None
    now = datetime.now(timezone.utc)
    run.status = RunStatus.RUNNING
    run.claimed_by = worker_id
    run.claimed_at = now
    run.heartbeat_at = now
    run.started_at = now
    run.attempts += 1
    await db.commit()
    await db.refresh(run)
    return run


async def reap_stale_runs(
    db: AsyncSession, max_attempts: int = 3, stale_seconds: int = 300
) -> int:
    """Requeue (or fail, at the attempt cap) runs whose worker stopped heartbeating."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_seconds)
    result = await db.execute(
        select(ExtractionRun).where(
            ExtractionRun.status == RunStatus.RUNNING,
            ExtractionRun.heartbeat_at < cutoff,
        )
    )
    stale = result.scalars().all()
    for run in stale:
        if run.attempts >= max_attempts:
            run.status = RunStatus.FAILED
            run.error_message = (run.error_message or "worker lost")[:4096]
            run.completed_at = datetime.now(timezone.utc)
        else:
            run.status = RunStatus.PENDING
            run.claimed_by = None
            run.claimed_at = None
    await db.commit()
    return len(stale)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && pytest tests/test_queue.py -v`
Expected: all 5 PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/queue.py backend/tests/test_queue.py
git commit -m "feat(queue): enqueue/claim/reap for the extraction job queue"
```

---

### Task 4: Scheduler tick service

**Files:**
- Create: `backend/app/services/scheduling.py`
- Test: `backend/tests/test_scheduler.py`

**Interfaces:**
- Consumes: `Schedule` (Task 1), `enqueue_run`/`reap_stale_runs`/`ActiveRunExists` (Task 3), `compute_next_run` (Task 2).
- Produces: `async tick(db, now: datetime | None = None) -> dict` returning `{"reaped": int, "enqueued": int, "due": int}`.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_scheduler.py`:

```python
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core.database import Base
from app.models import Vendor, DocumentationSource, ExtractionRun, Schedule
from app.models.extraction_run import RunStatus
from app.services.scheduling import tick

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"


@pytest_asyncio.fixture
async def sessions():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield factory
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def _source(db) -> uuid.UUID:
    v = Vendor(name="V")
    db.add(v)
    await db.flush()
    s = DocumentationSource(vendor_id=v.id, name="S", base_url="http://x")
    db.add(s)
    await db.commit()
    await db.refresh(s)
    return s.id


NOW = datetime(2026, 6, 17, 5, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_due_schedule_enqueues_and_advances(sessions):
    async with sessions() as db:
        sid = await _source(db)
        db.add(Schedule(
            source_id=sid, enabled=True, frequency="daily", time_of_day="02:00",
            cron="0 2 * * *", timezone="UTC",
            next_run_at=NOW - timedelta(minutes=1),
        ))
        await db.commit()
    async with sessions() as db:
        result = await tick(db, now=NOW)
        assert result["enqueued"] == 1
    async with sessions() as db:
        runs = (await db.execute(select(ExtractionRun))).scalars().all()
        assert len(runs) == 1
        assert runs[0].trigger == "scheduled"
        sched = (await db.execute(select(Schedule))).scalar_one()
        assert sched.next_run_at > NOW          # advanced to tomorrow 02:00
        assert sched.last_run_id == runs[0].id


@pytest.mark.asyncio
async def test_due_schedule_with_active_run_coalesces(sessions):
    async with sessions() as db:
        sid = await _source(db)
        db.add(ExtractionRun(source_id=sid, status=RunStatus.RUNNING))  # already active
        db.add(Schedule(
            source_id=sid, enabled=True, frequency="daily", time_of_day="02:00",
            cron="0 2 * * *", timezone="UTC", next_run_at=NOW - timedelta(minutes=1),
        ))
        await db.commit()
    async with sessions() as db:
        result = await tick(db, now=NOW)
        assert result["enqueued"] == 0  # coalesced
    async with sessions() as db:
        # No new pending run was created; the running one is untouched.
        pending = (await db.execute(
            select(ExtractionRun).where(ExtractionRun.status == RunStatus.PENDING)
        )).scalars().all()
        assert pending == []
        sched = (await db.execute(select(Schedule))).scalar_one()
        assert sched.next_run_at > NOW  # still advanced


@pytest.mark.asyncio
async def test_disabled_schedule_never_enqueues(sessions):
    async with sessions() as db:
        sid = await _source(db)
        db.add(Schedule(
            source_id=sid, enabled=False, frequency="daily", time_of_day="02:00",
            cron="0 2 * * *", timezone="UTC", next_run_at=NOW - timedelta(minutes=1),
        ))
        await db.commit()
    async with sessions() as db:
        result = await tick(db, now=NOW)
        assert result["enqueued"] == 0
        assert result["due"] == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && pytest tests/test_scheduler.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.scheduling'`.

- [ ] **Step 3: Write the implementation**

Create `backend/app/services/scheduling.py`:

```python
"""Scheduler tick: reap dead runs, enqueue due schedules, advance next_run_at."""

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.schedule import Schedule
from app.services.cron import compute_next_run
from app.services.queue import ActiveRunExists, enqueue_run, reap_stale_runs

logger = logging.getLogger(__name__)


async def tick(db: AsyncSession, now: datetime | None = None) -> dict:
    """One scheduler iteration. Idempotent and safe to call repeatedly."""
    now = now or datetime.now(timezone.utc)
    reaped = await reap_stale_runs(db)

    due = (
        await db.execute(
            select(Schedule).where(
                Schedule.enabled.is_(True), Schedule.next_run_at <= now
            )
        )
    ).scalars().all()

    enqueued = 0
    for sched in due:
        try:
            run = await enqueue_run(db, sched.source_id, trigger="scheduled")
            sched.last_run_at = now
            sched.last_run_id = run.id
            enqueued += 1
        except ActiveRunExists:
            logger.info(
                "Schedule for source %s coalesced — run already active",
                sched.source_id,
            )
        # Always advance: computing from `now` yields catch-up-once semantics.
        sched.next_run_at = compute_next_run(sched.cron, sched.timezone, now)
        await db.commit()

    return {"reaped": reaped, "enqueued": enqueued, "due": len(due)}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && pytest tests/test_scheduler.py -v`
Expected: all 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/scheduling.py backend/tests/test_scheduler.py
git commit -m "feat(scheduler): tick that reaps, enqueues due schedules, advances next_run_at"
```

---

### Task 5: Worker entrypoint

**Files:**
- Create: `backend/app/worker.py`
- Test: `backend/tests/test_worker.py`

**Interfaces:**
- Consumes: `claim_next_run` (Task 3), `firecrawl_service.extract_source`.
- Produces: `async run_one(claim_session_factory=None, work_session_factory=None) -> bool` (True if a run was claimed and handled), `async main_loop()`, module-level `WORKER_ID`. `run_one` accepts injectable session factories for testing; defaults to `app.core.database.async_session`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_worker.py`:

```python
import os
import sys
import uuid
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core.database import Base
from app.models import Vendor, DocumentationSource, ExtractionRun
from app.models.extraction_run import RunStatus
from app.services.queue import enqueue_run
import app.worker as worker

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"


@pytest_asyncio.fixture
async def sessions():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield factory
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def _source(db) -> uuid.UUID:
    v = Vendor(name="V")
    db.add(v)
    await db.flush()
    s = DocumentationSource(vendor_id=v.id, name="S", base_url="http://x")
    db.add(s)
    await db.commit()
    await db.refresh(s)
    return s.id


@pytest.mark.asyncio
async def test_run_one_empty_queue_returns_false(sessions):
    assert await worker.run_one(sessions, sessions) is False


@pytest.mark.asyncio
async def test_run_one_claims_and_calls_extract(sessions):
    async with sessions() as db:
        sid = await _source(db)
        await enqueue_run(db, sid)

    async def fake_extract(db, source_id, run_id=None):
        run = (await db.execute(
            select(ExtractionRun).where(ExtractionRun.id == run_id)
        )).scalar_one()
        run.status = RunStatus.COMPLETED

    with patch.object(
        worker.firecrawl_service, "extract_source",
        new=AsyncMock(side_effect=fake_extract),
    ) as m:
        handled = await worker.run_one(sessions, sessions)

    assert handled is True
    m.assert_awaited_once()
    async with sessions() as db:
        run = (await db.execute(select(ExtractionRun))).scalar_one()
        assert run.status == RunStatus.COMPLETED
        assert run.claimed_by == worker.WORKER_ID


@pytest.mark.asyncio
async def test_run_one_marks_failed_on_exception(sessions):
    async with sessions() as db:
        sid = await _source(db)
        await enqueue_run(db, sid)

    with patch.object(
        worker.firecrawl_service, "extract_source",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ):
        handled = await worker.run_one(sessions, sessions)

    assert handled is True
    async with sessions() as db:
        run = (await db.execute(select(ExtractionRun))).scalar_one()
        assert run.status == RunStatus.FAILED
        assert "boom" in (run.error_message or "")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/test_worker.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.worker'` (or attribute errors).

- [ ] **Step 3: Write the implementation**

Create `backend/app/worker.py`:

```python
"""Worker process: claim pending extraction runs and execute them.

Run with: python -m app.worker
"""

import asyncio
import logging
import socket
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update

# Ensure models are registered before any query runs.
import app.models  # noqa: F401
from app.core.database import async_session
from app.models.extraction_run import ExtractionRun, RunStatus
from app.services.firecrawl import firecrawl_service
from app.services.queue import claim_next_run

logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
logger = logging.getLogger(__name__)

POLL_INTERVAL = 2.0
HEARTBEAT_INTERVAL = 15.0
WORKER_ID = socket.gethostname()


async def _heartbeat(run_id: uuid.UUID, session_factory) -> None:
    """Bump heartbeat_at on its own session until cancelled."""
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL)
        try:
            async with session_factory() as db:
                await db.execute(
                    update(ExtractionRun)
                    .where(ExtractionRun.id == run_id)
                    .values(heartbeat_at=datetime.now(timezone.utc))
                )
                await db.commit()
        except Exception:  # heartbeat must never crash the worker
            logger.exception("Heartbeat update failed for run %s", run_id)


async def run_one(claim_session_factory=None, work_session_factory=None) -> bool:
    """Claim and execute one run. Returns True if a run was handled."""
    claim_session_factory = claim_session_factory or async_session
    work_session_factory = work_session_factory or async_session

    async with claim_session_factory() as db:
        run = await claim_next_run(db, WORKER_ID)
        if run is None:
            return False
        run_id, source_id = run.id, run.source_id

    hb = asyncio.create_task(_heartbeat(run_id, work_session_factory))
    try:
        async with work_session_factory() as db:
            await firecrawl_service.extract_source(db, source_id, run_id=run_id)
            await db.commit()
    except Exception as exc:
        logger.exception("Run %s failed", run_id)
        async with work_session_factory() as db:
            await db.rollback()
            res = await db.execute(
                select(ExtractionRun).where(ExtractionRun.id == run_id)
            )
            run = res.scalar_one_or_none()
            if run is not None and run.status not in (
                RunStatus.COMPLETED, RunStatus.FAILED,
            ):
                run.status = RunStatus.FAILED
                run.error_message = str(exc)[:4096]
                run.completed_at = datetime.now(timezone.utc)
                await db.commit()
    finally:
        hb.cancel()
    return True


async def main_loop() -> None:
    logger.info("Worker %s started", WORKER_ID)
    while True:
        try:
            handled = await run_one()
        except Exception:
            logger.exception("Worker loop error; backing off")
            handled = False
        if not handled:
            await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main_loop())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && pytest tests/test_worker.py -v`
Expected: all 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/worker.py backend/tests/test_worker.py
git commit -m "feat(worker): claim+execute loop with heartbeat and failure safety-net"
```

---

### Task 6: Scheduler entrypoint

**Files:**
- Create: `backend/app/scheduler.py`

**Interfaces:**
- Consumes: `tick` (Task 4), `async_session`.
- Produces: `async run_tick_once()` (one guarded tick), `async main_loop()`, module constants `TICK_INTERVAL`, `ADVISORY_LOCK_KEY`.

- [ ] **Step 1: Write the implementation**

Create `backend/app/scheduler.py`:

```python
"""Scheduler process: periodically reap dead runs and enqueue due schedules.

Run with: python -m app.scheduler  (deploy as a single replica)
"""

import asyncio
import logging

from sqlalchemy import text

# Ensure models are registered before any query runs.
import app.models  # noqa: F401
from app.core.database import async_session
from app.services.scheduling import tick

logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
logger = logging.getLogger(__name__)

TICK_INTERVAL = 30.0
# Belt-and-suspenders against an accidental second replica.
ADVISORY_LOCK_KEY = 778291


async def run_tick_once() -> None:
    async with async_session() as db:
        locked = (
            await db.execute(
                text("SELECT pg_try_advisory_lock(:k)"), {"k": ADVISORY_LOCK_KEY}
            )
        ).scalar()
        if not locked:
            logger.info("Another scheduler holds the lock; skipping tick")
            return
        try:
            result = await tick(db)
            if result["enqueued"] or result["reaped"]:
                logger.info("Tick: %s", result)
        finally:
            await db.execute(
                text("SELECT pg_advisory_unlock(:k)"), {"k": ADVISORY_LOCK_KEY}
            )
            await db.commit()


async def main_loop() -> None:
    logger.info("Scheduler started (tick=%ss)", TICK_INTERVAL)
    while True:
        try:
            await run_tick_once()
        except Exception:
            logger.exception("Scheduler tick error; will retry next interval")
        await asyncio.sleep(TICK_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main_loop())
```

- [ ] **Step 2: Verify it imports and starts cleanly**

Run: `cd backend && python -c "import app.scheduler; print(app.scheduler.TICK_INTERVAL, app.scheduler.ADVISORY_LOCK_KEY)"`
Expected: prints `30.0 778291` with no import error.

- [ ] **Step 3: Commit**

```bash
git add backend/app/scheduler.py
git commit -m "feat(scheduler): single-replica tick loop with advisory-lock guard"
```

---

### Task 7: Refactor manual trigger to enqueue; expose `trigger`

**Files:**
- Modify: `backend/app/routes/extraction.py`
- Test: `backend/tests/test_integration.py` (add an async route test; follow the `test_versions.py` fixture pattern)

**Interfaces:**
- Consumes: `enqueue_run`/`ActiveRunExists` (Task 3).
- Produces: `POST /api/extraction/trigger/{source_id}` now returns `status="pending"`; `/runs` and `/runs/{id}` responses include `"trigger"`.

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/test_integration.py` (or a new `tests/test_trigger_queue.py` using the `test_versions.py` async client fixture verbatim). Test body:

```python
@pytest.mark.asyncio
async def test_trigger_enqueues_pending_run(client):
    c, session_factory = client
    async with session_factory() as db:
        v = Vendor(name="V"); db.add(v); await db.flush()
        s = DocumentationSource(vendor_id=v.id, name="S", base_url="http://x")
        db.add(s); await db.commit(); await db.refresh(s)
        sid = str(s.id)

    r1 = await c.post(f"/api/extraction/trigger/{sid}")
    assert r1.status_code == 200
    assert r1.json()["status"] == "pending"

    # Second trigger while one is active -> 409 (coalesced by the DB invariant).
    r2 = await c.post(f"/api/extraction/trigger/{sid}")
    assert r2.status_code == 409

    runs = await c.get(f"/api/extraction/runs?source_id={sid}")
    assert runs.json()["runs"][0]["trigger"] == "manual"
```

(Import `Vendor`, `DocumentationSource` in the test module if not already.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/test_integration.py -k trigger_enqueues -v`
Expected: FAIL — current trigger returns `status="running"` and dispatches a background task (and `trigger` key is absent).

- [ ] **Step 3: Rewrite the trigger route**

In `backend/app/routes/extraction.py`, replace the `trigger_extraction` function and delete `_run_extraction_background` (its logic now lives in `app.worker`). New imports at top: remove `BackgroundTasks` from the fastapi import; add `from app.services.queue import enqueue_run, ActiveRunExists`. New route:

```python
@router.post("/trigger/{source_id}", response_model=ExtractionTriggerResponse)
async def trigger_extraction(
    source_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Queue a full extraction for a source. A worker picks it up.

    Poll /api/extraction/runs/{run_id} for status (pending -> running -> completed).
    """
    result = await db.execute(
        select(DocumentationSource).where(DocumentationSource.id == source_id)
    )
    source = result.scalar_one_or_none()
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")

    try:
        run = await enqueue_run(db, source_id, trigger="manual")
    except ActiveRunExists:
        raise HTTPException(
            status_code=409,
            detail="Extraction already queued or running for this source",
        )

    return ExtractionTriggerResponse(
        run_id=run.id,
        source_id=source_id,
        status="pending",
        message="Extraction queued. Poll /api/extraction/runs/{run_id} for progress.",
    )
```

Delete the entire `_run_extraction_background` function below it.

- [ ] **Step 4: Add `trigger` to the run status responses**

In the same file, add `"trigger": run.trigger,` to the dict returned by `get_run_status`, and `"trigger": r.trigger,` to each item in `list_runs`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend && pytest tests/test_integration.py -k trigger_enqueues -v`
Expected: PASS.

- [ ] **Step 6: Run the full backend suite to catch regressions**

Run: `cd backend && pytest -q`
Expected: all pass. If a pre-existing test asserted `status == "running"` on trigger or relied on background execution, update it to expect `"pending"` (the run now sits in the queue until a worker claims it).

- [ ] **Step 7: Commit**

```bash
git add backend/app/routes/extraction.py backend/tests/test_integration.py
git commit -m "refactor(extraction): trigger enqueues a pending run instead of running in-process"
```

---

### Task 8: Schedule schemas + CRUD routes

**Files:**
- Create: `backend/app/schemas/schedule.py`
- Modify: `backend/app/routes/sources.py`
- Test: `backend/tests/test_schedule_routes.py`

**Interfaces:**
- Consumes: `build_cron`/`compute_next_run` (Task 2), `Schedule` (Task 1).
- Produces: `GET/PUT/DELETE /api/sources/{id}/schedule`; `ScheduleConfig` (request), `ScheduleResponse` (response).

- [ ] **Step 1: Create the schemas**

Create `backend/app/schemas/schedule.py`:

```python
"""Schedule request/response schemas."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, field_validator


class ScheduleConfig(BaseModel):
    enabled: bool
    frequency: Literal["hourly", "daily", "weekly", "monthly"]
    time_of_day: str = "02:00"
    day_of_week: int | None = None    # 0-6, 0=Sunday (weekly)
    day_of_month: int | None = None   # 1-28 (monthly)
    timezone: str = "UTC"

    @field_validator("time_of_day")
    @classmethod
    def _valid_time(cls, v: str) -> str:
        hh, mm = v.split(":")
        if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
            raise ValueError("time_of_day must be HH:MM in 24h")
        return v

    @field_validator("day_of_week")
    @classmethod
    def _valid_dow(cls, v: int | None) -> int | None:
        if v is not None and not (0 <= v <= 6):
            raise ValueError("day_of_week must be 0-6")
        return v

    @field_validator("day_of_month")
    @classmethod
    def _valid_dom(cls, v: int | None) -> int | None:
        if v is not None and not (1 <= v <= 28):
            raise ValueError("day_of_month must be 1-28")
        return v


class ScheduleLastRun(BaseModel):
    id: uuid.UUID
    status: str
    completed_at: datetime | None


class ScheduleResponse(BaseModel):
    source_id: uuid.UUID
    enabled: bool
    frequency: str
    time_of_day: str
    day_of_week: int | None
    day_of_month: int | None
    cron: str
    timezone: str
    next_run_at: datetime | None
    last_run_at: datetime | None
    last_run: ScheduleLastRun | None
```

- [ ] **Step 2: Write the failing tests**

Create `backend/tests/test_schedule_routes.py` using the `test_versions.py` client fixture verbatim (copy the `client` fixture and `TEST_DATABASE_URL`). Tests:

```python
@pytest.mark.asyncio
async def test_get_schedule_404_when_none(client):
    c, sf = client
    async with sf() as db:
        v = Vendor(name="V"); db.add(v); await db.flush()
        s = DocumentationSource(vendor_id=v.id, name="S", base_url="http://x")
        db.add(s); await db.commit(); await db.refresh(s)
        sid = str(s.id)
    r = await c.get(f"/api/sources/{sid}/schedule")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_put_schedule_builds_cron_and_next_run(client):
    c, sf = client
    async with sf() as db:
        v = Vendor(name="V"); db.add(v); await db.flush()
        s = DocumentationSource(vendor_id=v.id, name="S", base_url="http://x")
        db.add(s); await db.commit(); await db.refresh(s)
        sid = str(s.id)
    body = {"enabled": True, "frequency": "daily", "time_of_day": "02:00", "timezone": "UTC"}
    r = await c.put(f"/api/sources/{sid}/schedule", json=body)
    assert r.status_code == 200
    data = r.json()
    assert data["cron"] == "0 2 * * *"
    assert data["enabled"] is True
    assert data["next_run_at"] is not None

    # Round-trips via GET, and PUT again upserts (no duplicate row error).
    g = await c.get(f"/api/sources/{sid}/schedule")
    assert g.json()["frequency"] == "daily"
    r2 = await c.put(f"/api/sources/{sid}/schedule",
                     json={**body, "frequency": "weekly", "day_of_week": 0})
    assert r2.json()["cron"] == "0 2 * * 0"


@pytest.mark.asyncio
async def test_disabled_schedule_has_null_next_run(client):
    c, sf = client
    async with sf() as db:
        v = Vendor(name="V"); db.add(v); await db.flush()
        s = DocumentationSource(vendor_id=v.id, name="S", base_url="http://x")
        db.add(s); await db.commit(); await db.refresh(s)
        sid = str(s.id)
    body = {"enabled": False, "frequency": "daily", "time_of_day": "02:00", "timezone": "UTC"}
    r = await c.put(f"/api/sources/{sid}/schedule", json=body)
    assert r.json()["next_run_at"] is None


@pytest.mark.asyncio
async def test_delete_schedule(client):
    c, sf = client
    async with sf() as db:
        v = Vendor(name="V"); db.add(v); await db.flush()
        s = DocumentationSource(vendor_id=v.id, name="S", base_url="http://x")
        db.add(s); await db.commit(); await db.refresh(s)
        sid = str(s.id)
    await c.put(f"/api/sources/{sid}/schedule",
                json={"enabled": True, "frequency": "daily", "time_of_day": "02:00", "timezone": "UTC"})
    d = await c.delete(f"/api/sources/{sid}/schedule")
    assert d.status_code == 204
    assert (await c.get(f"/api/sources/{sid}/schedule")).status_code == 404


@pytest.mark.asyncio
async def test_put_schedule_rejects_bad_time(client):
    c, sf = client
    async with sf() as db:
        v = Vendor(name="V"); db.add(v); await db.flush()
        s = DocumentationSource(vendor_id=v.id, name="S", base_url="http://x")
        db.add(s); await db.commit(); await db.refresh(s)
        sid = str(s.id)
    r = await c.put(f"/api/sources/{sid}/schedule",
                    json={"enabled": True, "frequency": "daily", "time_of_day": "99:99", "timezone": "UTC"})
    assert r.status_code == 422
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd backend && pytest tests/test_schedule_routes.py -v`
Expected: FAIL — schedule routes do not exist (404/405).

- [ ] **Step 4: Implement the routes**

In `backend/app/routes/sources.py`, add imports near the top:

```python
from datetime import datetime, timezone

from app.models.schedule import Schedule
from app.schemas.schedule import ScheduleConfig, ScheduleResponse, ScheduleLastRun
from app.services.cron import build_cron, compute_next_run
```

Add a helper and three routes (place after the existing source routes):

```python
async def _schedule_response(db: AsyncSession, sched: Schedule) -> ScheduleResponse:
    last_run = None
    if sched.last_run_id is not None:
        run = (
            await db.execute(
                select(ExtractionRun).where(ExtractionRun.id == sched.last_run_id)
            )
        ).scalar_one_or_none()
        if run is not None:
            last_run = ScheduleLastRun(
                id=run.id, status=run.status.value, completed_at=run.completed_at
            )
    return ScheduleResponse(
        source_id=sched.source_id,
        enabled=sched.enabled,
        frequency=sched.frequency,
        time_of_day=sched.time_of_day,
        day_of_week=sched.day_of_week,
        day_of_month=sched.day_of_month,
        cron=sched.cron,
        timezone=sched.timezone,
        next_run_at=sched.next_run_at,
        last_run_at=sched.last_run_at,
        last_run=last_run,
    )


@router.get("/{source_id}/schedule", response_model=ScheduleResponse)
async def get_schedule(source_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    sched = (
        await db.execute(select(Schedule).where(Schedule.source_id == source_id))
    ).scalar_one_or_none()
    if sched is None:
        raise HTTPException(status_code=404, detail="No schedule for this source")
    return await _schedule_response(db, sched)


@router.put("/{source_id}/schedule", response_model=ScheduleResponse)
async def put_schedule(
    source_id: uuid.UUID,
    body: ScheduleConfig,
    db: AsyncSession = Depends(get_db),
):
    source = (
        await db.execute(
            select(DocumentationSource).where(DocumentationSource.id == source_id)
        )
    ).scalar_one_or_none()
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")

    cron = build_cron(
        body.frequency, body.time_of_day, body.day_of_week, body.day_of_month
    )
    now = datetime.now(timezone.utc)
    next_run_at = compute_next_run(cron, body.timezone, now) if body.enabled else None

    sched = (
        await db.execute(select(Schedule).where(Schedule.source_id == source_id))
    ).scalar_one_or_none()
    if sched is None:
        sched = Schedule(source_id=source_id)
        db.add(sched)
    sched.enabled = body.enabled
    sched.frequency = body.frequency
    sched.time_of_day = body.time_of_day
    sched.day_of_week = body.day_of_week
    sched.day_of_month = body.day_of_month
    sched.cron = cron
    sched.timezone = body.timezone
    sched.next_run_at = next_run_at
    await db.commit()
    await db.refresh(sched)
    return await _schedule_response(db, sched)


@router.delete("/{source_id}/schedule", status_code=204)
async def delete_schedule(source_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    sched = (
        await db.execute(select(Schedule).where(Schedule.source_id == source_id))
    ).scalar_one_or_none()
    if sched is not None:
        await db.delete(sched)
        await db.commit()
    return None
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend && pytest tests/test_schedule_routes.py -v`
Expected: all 5 PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/schemas/schedule.py backend/app/routes/sources.py backend/tests/test_schedule_routes.py
git commit -m "feat(api): per-source schedule CRUD endpoints"
```

---

### Task 9: Compose services + process wiring

**Files:**
- Modify: `docker-compose.yml`

**Interfaces:**
- Produces: `worker` and `scheduler` services from the same `./backend` image with `command:` overrides.

- [ ] **Step 1: Add the worker and scheduler services**

In `docker-compose.yml`, add two services that reuse the backend build context and env. They depend on Postgres being healthy; the loops retry-connect until the web service has run migrations (no migration runs here). Add after the `backend` service:

```yaml
  worker:
    build: ./backend
    command: ["python", "-m", "app.worker"]
    environment:
      DOCEXTRACTOR_DATABASE_URL: postgresql+asyncpg://docextractor:docextractor_dev@postgres:5432/docextractor
      DOCEXTRACTOR_DATABASE_URL_SYNC: postgresql+psycopg2://docextractor:docextractor_dev@postgres:5432/docextractor
      DOCEXTRACTOR_FIRECRAWL_API_URL: http://firecrawl.k3s.home.lan
      DOCEXTRACTOR_FIRECRAWL_API_KEY: fc-bf48f20724d6459cbdda97aef48a41fb
      DOCEXTRACTOR_WEBHOOK_BASE_URL: http://172.16.255.190:8000
    volumes:
      - exports_data:/app/exports
      - media_data:/app/media
    depends_on:
      postgres:
        condition: service_healthy
      backend:
        condition: service_started

  scheduler:
    build: ./backend
    command: ["python", "-m", "app.scheduler"]
    environment:
      DOCEXTRACTOR_DATABASE_URL: postgresql+asyncpg://docextractor:docextractor_dev@postgres:5432/docextractor
      DOCEXTRACTOR_DATABASE_URL_SYNC: postgresql+psycopg2://docextractor:docextractor_dev@postgres:5432/docextractor
      DOCEXTRACTOR_FIRECRAWL_API_URL: http://firecrawl.k3s.home.lan
      DOCEXTRACTOR_FIRECRAWL_API_KEY: fc-bf48f20724d6459cbdda97aef48a41fb
    depends_on:
      postgres:
        condition: service_healthy
      backend:
        condition: service_started
```

Note: the worker mounts `exports_data` + `media_data` because `extract_source` writes images to `media/`; the scheduler needs neither (it only touches the DB).

- [ ] **Step 2: Build and start the new services**

Run: `docker compose up -d --build backend worker scheduler`
Expected: all three come up. Then:
Run: `docker compose logs scheduler | tail -n 5`
Expected: a line like `Scheduler started (tick=30.0s)`.
Run: `docker compose logs worker | tail -n 5`
Expected: a line like `Worker <host> started`.

- [ ] **Step 3: Commit**

```bash
git add docker-compose.yml
git commit -m "chore(compose): add worker and scheduler services from the backend image"
```

---

### Task 10: Frontend — types, client, ScheduleControl, queued state

**Files:**
- Modify: `frontend/src/types/index.ts`
- Modify: `frontend/src/api/client.ts`
- Create: `frontend/src/components/ScheduleControl.tsx`
- Modify: the source view that renders per-source panels (the component that shows `ExportPanel`/`ChangelogPanel`; wire `ScheduleControl` alongside them)
- Modify: `frontend/src/App.css` (styles for the schedule control + a PENDING run chip)

**Interfaces:**
- Consumes: `GET/PUT/DELETE /api/sources/{id}/schedule`, `ScheduleResponse` shape (Task 8).
- Produces: `Schedule`, `ScheduleConfig` TS types; `getSchedule`, `putSchedule`, `deleteSchedule` client functions; `<ScheduleControl source={...} />`.

- [ ] **Step 1: Add types**

In `frontend/src/types/index.ts`, extend `ExtractionRun.status` and add `trigger`, then add schedule types:

```typescript
// in ExtractionRun: change status union and add trigger
  status: "pending" | "running" | "completed" | "failed" | "cancelled";
  trigger?: "manual" | "scheduled";
```
Append:
```typescript
export type Frequency = "hourly" | "daily" | "weekly" | "monthly";

export interface ScheduleConfig {
  enabled: boolean;
  frequency: Frequency;
  time_of_day: string;        // HH:MM
  day_of_week?: number | null;
  day_of_month?: number | null;
  timezone: string;
}

export interface Schedule extends ScheduleConfig {
  source_id: string;
  cron: string;
  next_run_at: string | null;
  last_run_at: string | null;
  last_run: { id: string; status: string; completed_at: string | null } | null;
}
```

- [ ] **Step 2: Add client functions**

In `frontend/src/api/client.ts`, add (matching the existing axios style):

```typescript
export async function getSchedule(sourceId: string): Promise<Schedule | null> {
  try {
    const { data } = await api.get<Schedule>(`/api/sources/${sourceId}/schedule`);
    return data;
  } catch (e: any) {
    if (e?.response?.status === 404) return null;
    throw e;
  }
}

export async function putSchedule(
  sourceId: string, config: ScheduleConfig,
): Promise<Schedule> {
  const { data } = await api.put<Schedule>(`/api/sources/${sourceId}/schedule`, config);
  return data;
}

export async function deleteSchedule(sourceId: string): Promise<void> {
  await api.delete(`/api/sources/${sourceId}/schedule`);
}
```
(Import `Schedule`, `ScheduleConfig` from `../types`, and reuse the existing axios instance name — check the file for whether it is `api` or `client` and match it.)

- [ ] **Step 3: Build the `ScheduleControl` component**

Create `frontend/src/components/ScheduleControl.tsx`:

```tsx
import { useEffect, useState } from "react";
import type { DocumentationSource, Frequency, ScheduleConfig } from "../types";
import { getSchedule, putSchedule } from "../api/client";

const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

export default function ScheduleControl({ source }: { source: DocumentationSource }) {
  const browserTz = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  const [cfg, setCfg] = useState<ScheduleConfig>({
    enabled: false, frequency: "daily", time_of_day: "02:00",
    day_of_week: 0, day_of_month: 1, timezone: browserTz,
  });
  const [nextRun, setNextRun] = useState<string | null>(null);
  const [lastRun, setLastRun] = useState<Schedule["last_run"]>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    getSchedule(source.id).then((s) => {
      if (s) {
        setCfg({
          enabled: s.enabled, frequency: s.frequency as Frequency,
          time_of_day: s.time_of_day, day_of_week: s.day_of_week ?? 0,
          day_of_month: s.day_of_month ?? 1, timezone: s.timezone,
        });
        setNextRun(s.next_run_at);
        setLastRun(s.last_run);
      }
    }).catch(() => setError("Failed to load schedule"));
  }, [source.id]);

  const save = async () => {
    setSaving(true); setError("");
    try {
      const s = await putSchedule(source.id, cfg);
      setNextRun(s.next_run_at);
    } catch {
      setError("Failed to save schedule");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="schedule-control">
      <label className="schedule-toggle">
        <input
          type="checkbox"
          checked={cfg.enabled}
          onChange={(e) => setCfg({ ...cfg, enabled: e.target.checked })}
        />
        Run on a schedule
      </label>

      {cfg.enabled && (
        <div className="schedule-fields">
          <select
            value={cfg.frequency}
            onChange={(e) => setCfg({ ...cfg, frequency: e.target.value as Frequency })}
          >
            <option value="hourly">Hourly</option>
            <option value="daily">Daily</option>
            <option value="weekly">Weekly</option>
            <option value="monthly">Monthly</option>
          </select>

          {cfg.frequency === "weekly" && (
            <select
              value={cfg.day_of_week ?? 0}
              onChange={(e) => setCfg({ ...cfg, day_of_week: Number(e.target.value) })}
            >
              {DAYS.map((d, i) => <option key={i} value={i}>{d}</option>)}
            </select>
          )}

          {cfg.frequency === "monthly" && (
            <select
              value={cfg.day_of_month ?? 1}
              onChange={(e) => setCfg({ ...cfg, day_of_month: Number(e.target.value) })}
            >
              {Array.from({ length: 28 }, (_, i) => i + 1).map((d) =>
                <option key={d} value={d}>{d}</option>)}
            </select>
          )}

          {cfg.frequency !== "hourly" && (
            <input
              type="time"
              value={cfg.time_of_day}
              onChange={(e) => setCfg({ ...cfg, time_of_day: e.target.value })}
            />
          )}
          {cfg.frequency === "hourly" && (
            <span className="hint">at minute {cfg.time_of_day.split(":")[1]}</span>
          )}

          <span className="schedule-tz">{cfg.timezone}</span>
        </div>
      )}

      <button onClick={save} disabled={saving}>
        {saving ? "Saving…" : "Save schedule"}
      </button>
      {nextRun && cfg.enabled && (
        <p className="hint">Next run: {new Date(nextRun).toLocaleString()}</p>
      )}
      {lastRun && (
        <p className="hint">Last run: {lastRun.status}</p>
      )}
      {error && <p className="error">{error}</p>}
    </div>
  );
}
```
(Add `import type { Schedule } from "../types";` — the `lastRun` state references it.)

- [ ] **Step 4: Wire `ScheduleControl` into the source view**

Find the component that renders the per-source panels (search for where `ExportPanel` is rendered):
Run: `cd frontend && grep -rn "ExportPanel" src`
In that component, import and render `<ScheduleControl source={source} />` next to the existing panels (pass the same `source`/source object already in scope).

- [ ] **Step 5: Add a PENDING chip to the run-status display**

Find where run `status` is rendered (search for `"running"` / a status badge):
Run: `cd frontend && grep -rn "running" src/components`
Where statuses map to labels/badges, add a `pending` → "Queued…" case (mirror the existing `running` styling). Add a `.run-pending` (or reuse existing badge classes) rule in `App.css`.

- [ ] **Step 6: Add minimal styles**

In `frontend/src/App.css`, add styles consistent with the existing design system (petrol-ink surfaces, signal-amber accents) for `.schedule-control`, `.schedule-toggle`, `.schedule-fields`, `.schedule-tz`, and the pending chip. Match the spacing/typography of `.export-panel`/`.chapter-toggle` already in the file.

- [ ] **Step 7: Type-check, build, lint**

Run: `cd frontend && npm run build && npm run lint`
Expected: build succeeds, no type errors, lint clean.

- [ ] **Step 8: Commit**

```bash
git add frontend/src/types/index.ts frontend/src/api/client.ts frontend/src/components/ScheduleControl.tsx frontend/src/App.css
git add -A frontend/src
git commit -m "feat(ui): per-source schedule control and queued run state"
```

---

### Task 11: End-to-end verification on live data

**Files:** none (verification only).

- [ ] **Step 1: Rebuild and bring up the full stack**

Run: `docker compose up -d --build`
Expected: postgres, backend, worker, scheduler, frontend all healthy.

- [ ] **Step 2: Verify manual trigger flows through the queue**

Trigger an extraction for the Clumio source via the UI (or `POST /api/extraction/trigger/{source_id}`), then poll `GET /api/extraction/runs/{run_id}`.
Expected: status transitions `pending` → `running` (a worker claimed it) → `completed`; `trigger` is `"manual"`; `claimed_by` is the worker hostname.

- [ ] **Step 3: Verify the schedule fires + coalesces + advances**

Set a schedule ~2 minutes in the future (e.g. daily at the next minute) via the UI. Watch `docker compose logs -f scheduler`.
Expected: at the boundary the scheduler enqueues one run (`Tick: {'reaped': 0, 'enqueued': 1, 'due': 1}`); if a run is already active it logs "coalesced"; `GET /api/sources/{id}/schedule` shows `next_run_at` advanced to the following day.

- [ ] **Step 4: Verify reaper requeues a dead worker's run**

Start an extraction, then `docker compose kill worker` mid-run. Within ~5 minutes the scheduler reaps it.
Expected: the run returns to `pending` (then re-runs once `worker` is restarted with `docker compose up -d worker`), or `failed` after `max_attempts`.

- [ ] **Step 5: Final full test sweep**

Run: `cd backend && pytest -q` and `cd frontend && npm run build && npm run lint`
Expected: backend suite green (43 prior + new cron/queue/scheduler/worker/route/schedule tests), frontend builds and lints clean.

---

## Self-Review

**Spec coverage:**
- Process topology (web/worker/scheduler, one image) → Tasks 5, 6, 9.
- Webhook stays on web → unchanged by design; trigger refactor (Task 7) leaves the webhook route intact.
- `ExtractionRun`-as-queue + `schedules` table + partial unique index + queue columns → Task 1.
- Friendly presets → cron + timezone → Tasks 2, 8 (friendly fields persisted for UI reconstruction — a deliberate refinement over "cron only," noted in the model/schema).
- Skip/coalesce (one active run per source) → DB invariant in Task 1, exercised in Tasks 3, 4, 7.
- Catch-up-once → `compute_next_run` from `now` (Task 2) + tick (Task 4).
- SKIP LOCKED claim, heartbeat, reaper, retries (`attempts`) → Tasks 3, 5.
- Manual trigger becomes async-via-queue, same contract, `trigger` exposed → Task 7.
- Schedule CRUD API + frontend control + PENDING state → Tasks 8, 10.
- Migrations on web only; worker/scheduler retry-connect → Task 9 (depends_on + loop-level retry in Tasks 5/6 `main_loop` try/except).
- Tests per the repo's async fixture; live Clumio verification → Tasks 2–8, 11.

**Placeholder scan:** No TBD/TODO; every code step contains full code. Two intentionally directed (not vague) steps in Task 10 (4, 5, 6) say "search for where X is rendered" because the exact host component/className isn't known without inspecting the frontend at execution time — each gives the exact `grep` to run and the exact edit to make.

**Type consistency:** `enqueue_run(db, source_id, trigger)`, `claim_next_run(db, worker_id)`, `reap_stale_runs(db, max_attempts, stale_seconds)`, `tick(db, now)`, `build_cron(frequency, time_of_day, day_of_week, day_of_month)`, `compute_next_run(cron, timezone, after)`, `run_one(claim_session_factory, work_session_factory)` are used identically across tasks and tests. `RunStatus.PENDING`/`.value=="pending"` aligns with the frontend `status` union. Enum DB labels are uppercase in every SQL predicate and the migration.

## Out of scope (carried from the spec)
Kubernetes manifests/Helm; PDF export; multiple schedules per source; raw-cron UI field.
