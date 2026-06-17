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
        # Deviation: heartbeat_at set to now so reap_stale_runs treats it as healthy
        db.add(ExtractionRun(source_id=sid, status=RunStatus.RUNNING, heartbeat_at=datetime.now(timezone.utc)))
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
