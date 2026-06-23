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
from app.models import Vendor, Product, DocumentationSource, ExtractionRun
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
    s_prod = Product(vendor_id=v.id, name="P")
    db.add(s_prod)
    await db.flush()
    s = DocumentationSource(product_id=s_prod.id, name="S", base_url="http://x")
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
        assert run.claimed_by == worker.WORKER_ID
