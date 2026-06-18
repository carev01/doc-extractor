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
from app.services.queue import claim_next_run, claim_next_export
from app.services.export_runner import run_export_job_sync

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


async def _heartbeat_export(job_id: uuid.UUID, session_factory) -> None:
    """Bump ExportJob.heartbeat_at on its own session until cancelled."""
    from app.models.export_job import ExportJob
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL)
        try:
            async with session_factory() as db:
                await db.execute(
                    update(ExportJob)
                    .where(ExportJob.id == job_id)
                    .values(heartbeat_at=datetime.now(timezone.utc))
                )
                await db.commit()
        except Exception:
            logger.exception("Export heartbeat failed for %s", job_id)


async def run_one(claim_session_factory=None, work_session_factory=None) -> bool:
    """Claim and execute one run. Returns True if a run was handled."""
    claim_session_factory = claim_session_factory or async_session
    work_session_factory = work_session_factory or async_session

    # 1) Try to claim an extraction run — open, claim, close.
    async with claim_session_factory() as db:
        run = await claim_next_run(db, WORKER_ID)
        run_id = run.id if run else None
        source_id = run.source_id if run else None
    # claim session is now closed.

    if run_id is not None:
        hb = asyncio.create_task(_heartbeat(run_id, work_session_factory))
        try:
            async with work_session_factory() as db:
                await firecrawl_service.extract_source(db, source_id, run_id=run_id)
                await db.commit()
        except Exception as exc:
            logger.exception("Run %s failed", run_id)
            async with work_session_factory() as db:
                res = await db.execute(
                    select(ExtractionRun).where(ExtractionRun.id == run_id)
                )
                r = res.scalar_one_or_none()
                if r is not None and r.status not in (
                    RunStatus.COMPLETED, RunStatus.FAILED,
                ):
                    r.status = RunStatus.FAILED
                    r.error_message = str(exc)[:4096]
                    r.completed_at = datetime.now(timezone.utc)
                    await db.commit()
        finally:
            hb.cancel()
        return True

    # 2) No extraction run — try an export job — open, claim, close.
    async with claim_session_factory() as db:
        job = await claim_next_export(db, WORKER_ID)
        job_id = job.id if job else None
    # claim session is now closed.

    if job_id is None:
        return False

    hb = asyncio.create_task(_heartbeat_export(job_id, work_session_factory))
    try:
        # Generation is synchronous; run it off the event loop.
        await asyncio.to_thread(run_export_job_sync, job_id)
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
