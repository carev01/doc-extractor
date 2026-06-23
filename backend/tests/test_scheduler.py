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
from app.models import Vendor, Product, DocumentationSource, ExtractionRun, Job, JobRun
from app.models.extraction_run import RunStatus
from app.models.job_run import JobRunStatus
from app.services.scheduling import tick, reconcile_job_runs

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


async def _source(db, job_id=None) -> uuid.UUID:
    v = Vendor(name="V")
    db.add(v)
    await db.flush()
    prod = Product(vendor_id=v.id, name="P")
    db.add(prod)
    await db.flush()
    s = DocumentationSource(product_id=prod.id, name="S", base_url="http://x", job_id=job_id)
    db.add(s)
    await db.commit()
    await db.refresh(s)
    return s.id


async def _job(db, enabled=True, next_run_at=None) -> uuid.UUID:
    job = Job(
        name="Nightly", enabled=enabled, frequency="daily", time_of_day="02:00",
        cron="0 2 * * *", timezone="UTC", next_run_at=next_run_at,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)
    return job.id


NOW = datetime(2026, 6, 17, 5, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_due_job_fans_out_and_advances(sessions):
    async with sessions() as db:
        jid = await _job(db, next_run_at=NOW - timedelta(minutes=1))
        await _source(db, job_id=jid)
        await _source(db, job_id=jid)
    async with sessions() as db:
        result = await tick(db, now=NOW)
        assert result["enqueued"] == 1   # one job fired
    async with sessions() as db:
        runs = (await db.execute(select(ExtractionRun))).scalars().all()
        assert len(runs) == 2            # one child run per assigned source
        assert all(r.trigger == "scheduled" and r.job_run_id is not None for r in runs)
        jr = (await db.execute(select(JobRun))).scalar_one()
        assert jr.sources_total == 2
        job = (await db.execute(select(Job))).scalar_one()
        assert job.next_run_at > NOW     # advanced to tomorrow 02:00
        assert job.last_run_at == NOW


@pytest.mark.asyncio
async def test_due_job_with_active_run_coalesces_that_source(sessions):
    async with sessions() as db:
        jid = await _job(db, next_run_at=NOW - timedelta(minutes=1))
        s1 = await _source(db, job_id=jid)
        s2 = await _source(db, job_id=jid)
        # s1 already has a healthy running run — it should be coalesced.
        db.add(ExtractionRun(
            source_id=s1, status=RunStatus.RUNNING,
            heartbeat_at=datetime.now(timezone.utc),
        ))
        await db.commit()
    async with sessions() as db:
        await tick(db, now=NOW)
    async with sessions() as db:
        pending = (await db.execute(
            select(ExtractionRun).where(ExtractionRun.status == RunStatus.PENDING)
        )).scalars().all()
        assert len(pending) == 1                 # only s2 enqueued
        assert pending[0].source_id == s2
        jr = (await db.execute(select(JobRun))).scalar_one()
        assert jr.sources_total == 1             # coalesced source not counted


@pytest.mark.asyncio
async def test_disabled_job_never_fires(sessions):
    async with sessions() as db:
        jid = await _job(db, enabled=False, next_run_at=NOW - timedelta(minutes=1))
        await _source(db, job_id=jid)
    async with sessions() as db:
        result = await tick(db, now=NOW)
        assert result["enqueued"] == 0 and result["due"] == 0


@pytest.mark.asyncio
async def test_reconcile_completes_job_run_when_children_finish(sessions):
    async with sessions() as db:
        jid = await _job(db, next_run_at=NOW - timedelta(minutes=1))
        await _source(db, job_id=jid)
        await _source(db, job_id=jid)
    async with sessions() as db:
        await tick(db, now=NOW)
    async with sessions() as db:
        for r in (await db.execute(select(ExtractionRun))).scalars().all():
            r.status = RunStatus.COMPLETED
        await db.commit()
    async with sessions() as db:
        changed = await reconcile_job_runs(db, now=NOW)
        assert changed == 1
        jr = (await db.execute(select(JobRun))).scalar_one()
        assert jr.status == JobRunStatus.COMPLETED
        assert jr.sources_done == 2 and jr.completed_at is not None


@pytest.mark.asyncio
async def test_reconcile_marks_partial_on_mixed_outcome(sessions):
    async with sessions() as db:
        jid = await _job(db, next_run_at=NOW - timedelta(minutes=1))
        await _source(db, job_id=jid)
        await _source(db, job_id=jid)
    async with sessions() as db:
        await tick(db, now=NOW)
    async with sessions() as db:
        runs = (await db.execute(select(ExtractionRun))).scalars().all()
        runs[0].status = RunStatus.COMPLETED
        runs[1].status = RunStatus.FAILED
        await db.commit()
    async with sessions() as db:
        await reconcile_job_runs(db, now=NOW)
        jr = (await db.execute(select(JobRun))).scalar_one()
        assert jr.status == JobRunStatus.PARTIAL
        assert jr.sources_done == 1 and jr.sources_failed == 1
