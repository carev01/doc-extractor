"""Exports run alongside extractions, excluded per source.

The worker used to take one job at a time and look at the export queue only when
no extraction was waiting, so a busy night held every export until the whole
extraction queue drained. Exports now have their own lane, and the only thing
that holds one back is its own source being extracted *right now*:

* an export is not claimed while its source has a RUNNING extraction (it stays
  PENDING and starts when the run ends) — a PENDING or PAUSED run does not
  block it, so a Tier 1 firing that queues 112 runs doesn't hold exports for
  hours;
* symmetrically, an extraction is not claimed while an export of its source is
  RUNNING, so a queued run can't start rewriting articles mid-export.
"""

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
from tests.db_reset import reset_schema
from app.models import DocumentationSource, ExtractionRun, Product, Vendor
from app.models.export_job import ExportJob, ExportStatus
from app.models.extraction_run import RunStatus
from app.services.queue import (
    claim_next_export, claim_next_run, enqueue_export, enqueue_run,
    sources_extracting,
)
import app.worker as worker

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def sessions():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(reset_schema)
    yield factory
    await engine.dispose()


async def _sources(db, n=2) -> list[uuid.UUID]:
    v = Vendor(name="V"); db.add(v); await db.flush()
    p = Product(vendor_id=v.id, name="P"); db.add(p); await db.flush()
    ids = []
    for i in range(n):
        s = DocumentationSource(product_id=p.id, name=f"S{i}", base_url=f"http://x/{i}")
        db.add(s); await db.flush()
        ids.append(s.id)
    await db.commit()
    return ids


async def _run(db, sid, status) -> ExtractionRun:
    r = ExtractionRun(source_id=sid, status=status)
    db.add(r); await db.commit()
    return r


async def test_export_waits_while_its_source_is_extracting(sessions):
    async with sessions() as db:
        busy, idle = await _sources(db)
        await _run(db, busy, RunStatus.RUNNING)
        blocked = await enqueue_export(db, busy, {"format": "markdown"})
        free = await enqueue_export(db, idle, {"format": "markdown"})

        # The older job is skipped, not failed; the other source's export runs.
        claimed = await claim_next_export(db, "w")
        assert claimed.id == free.id
        assert await claim_next_export(db, "w") is None
        await db.refresh(blocked)
        assert blocked.status == ExportStatus.PENDING


@pytest.mark.parametrize("status", [RunStatus.PENDING, RunStatus.PAUSED,
                                    RunStatus.COMPLETED, RunStatus.FAILED])
async def test_only_a_running_extraction_blocks_an_export(sessions, status):
    async with sessions() as db:
        (sid,) = await _sources(db, 1)
        await _run(db, sid, status)
        job = await enqueue_export(db, sid, {"format": "markdown"})
        claimed = await claim_next_export(db, "w")
        assert claimed is not None and claimed.id == job.id


async def test_export_starts_once_the_run_finishes(sessions):
    async with sessions() as db:
        (sid,) = await _sources(db, 1)
        run = await _run(db, sid, RunStatus.RUNNING)
        job = await enqueue_export(db, sid, {"format": "markdown"})
        assert await claim_next_export(db, "w") is None
        run.status = RunStatus.COMPLETED
        await db.commit()
        assert (await claim_next_export(db, "w")).id == job.id


async def test_extraction_waits_while_its_source_is_exporting(sessions):
    async with sessions() as db:
        exporting, other = await _sources(db)
        await enqueue_export(db, exporting, {"format": "markdown"})
        assert await claim_next_export(db, "w") is not None     # now RUNNING
        first = await enqueue_run(db, exporting)
        second = await enqueue_run(db, other)

        claimed = await claim_next_run(db, "w")
        assert claimed.id == second.id                          # FIFO skips, not stalls
        assert await claim_next_run(db, "w") is None
        await db.refresh(first)
        assert first.status == RunStatus.PENDING

        job = (await db.execute(select(ExportJob))).scalar_one()
        job.status = ExportStatus.COMPLETED
        await db.commit()
        assert (await claim_next_run(db, "w")).id == first.id


async def test_sources_extracting_reports_running_sources_only(sessions):
    async with sessions() as db:
        a, b = await _sources(db)
        await _run(db, a, RunStatus.RUNNING)
        await _run(db, b, RunStatus.PENDING)
        assert await sources_extracting(db, [a, b]) == {a}
        assert await sources_extracting(db, []) == set()


async def test_export_lane_runs_an_export_while_an_extraction_is_queued(sessions):
    # The old single loop only reached the export queue when no extraction was
    # claimable; the export lane must not care.
    async with sessions() as db:
        busy, other = await _sources(db)
        await enqueue_run(db, busy)
        job = await enqueue_export(db, other, {"format": "markdown"})

    ran = []
    with patch.object(worker, "run_export_job_sync", side_effect=lambda jid: ran.append(jid)):
        handled = await worker.run_one_export(sessions, sessions)

    assert handled is True and ran == [job.id]
    async with sessions() as db:
        run = (await db.execute(select(ExtractionRun))).scalar_one()
        assert run.status == RunStatus.PENDING          # untouched by the export lane


async def test_extraction_lane_never_takes_an_export(sessions):
    async with sessions() as db:
        (sid,) = await _sources(db, 1)
        await enqueue_export(db, sid, {"format": "markdown"})

    with patch.object(worker.firecrawl_service, "extract_source", new=AsyncMock()):
        assert await worker.run_one_extraction(sessions, sessions) is False
    async with sessions() as db:
        job = (await db.execute(select(ExportJob))).scalar_one()
        assert job.status == ExportStatus.PENDING


async def test_export_logs_stay_out_of_a_concurrent_runs_log(sessions):
    # The extraction lane captures root-logger records into the run's log_text;
    # an export generating on its own thread at the same time must not land there.
    import logging
    handler = worker._RunLogHandler()
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        async with sessions() as db:
            (sid,) = await _sources(db, 1)
            await enqueue_export(db, sid, {"format": "markdown"})

        def noisy(job_id):
            logging.getLogger("app.services.exporter").warning("export-line")

        with patch.object(worker, "run_export_job_sync", side_effect=noisy):
            assert await worker.run_one_export(sessions, sessions) is True
        logging.getLogger("app.services.firecrawl").warning("extraction-line")
    finally:
        root.removeHandler(handler)
    text = handler.drain()
    assert "extraction-line" in text          # the lane flag was reset afterwards
    assert "export-line" not in text
