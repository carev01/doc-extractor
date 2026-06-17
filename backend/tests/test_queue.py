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
    vendor = Vendor(name=f"V-{uuid.uuid4().hex[:8]}")
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
