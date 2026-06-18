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
from app.models import Vendor, DocumentationSource, ExportJob
from app.models.export_job import ExportStatus
from app.services.queue import enqueue_export, claim_next_export, reap_stale_exports

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
async def test_enqueue_export_creates_pending(sessions):
    async with sessions() as db:
        sid = await _source(db)
        job = await enqueue_export(db, sid, {"source_id": str(sid), "format": "pdf"})
        assert job.status == ExportStatus.PENDING
        assert job.request["format"] == "pdf"


@pytest.mark.asyncio
async def test_claim_marks_running(sessions):
    async with sessions() as db:
        sid = await _source(db)
        await enqueue_export(db, sid, {"source_id": str(sid)})
    async with sessions() as db:
        job = await claim_next_export(db, "worker-1")
        assert job is not None and job.status == ExportStatus.RUNNING
        assert job.claimed_by == "worker-1" and job.attempts == 1 and job.heartbeat_at is not None
    async with sessions() as db:
        assert await claim_next_export(db, "worker-2") is None


@pytest.mark.asyncio
async def test_reap_requeues_then_fails_at_cap(sessions):
    async with sessions() as db:
        sid = await _source(db)
        job = ExportJob(
            source_id=sid, request={}, status=ExportStatus.RUNNING, attempts=1,
            heartbeat_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        )
        db.add(job)
        await db.commit()
        jid = job.id
    async with sessions() as db:
        assert await reap_stale_exports(db) == 1
    async with sessions() as db:
        job = (await db.execute(select(ExportJob).where(ExportJob.id == jid))).scalar_one()
        assert job.status == ExportStatus.PENDING and job.claimed_by is None
    async with sessions() as db:
        job = (await db.execute(select(ExportJob).where(ExportJob.id == jid))).scalar_one()
        job.status = ExportStatus.RUNNING
        job.attempts = 3
        job.heartbeat_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        await db.commit()
    async with sessions() as db:
        await reap_stale_exports(db)
    async with sessions() as db:
        job = (await db.execute(select(ExportJob).where(ExportJob.id == jid))).scalar_one()
        assert job.status == ExportStatus.FAILED
