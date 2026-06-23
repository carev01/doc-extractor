"""Worker process: claim pending extraction runs and execute them.

Run with: python -m app.worker
"""

import asyncio
import logging
import socket
import threading
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select, update

# Ensure models are registered before any query runs.
import app.models  # noqa: F401
from app.core.database import async_session
from app.models.extraction_run import ExtractionRun, RunStatus
from app.models.source import DocumentationSource, SourceStatus
from app.services.firecrawl import firecrawl_service
from app.services.queue import claim_next_run, claim_next_export
from app.services.export_runner import run_export_job_sync

logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
logger = logging.getLogger(__name__)

POLL_INTERVAL = 2.0
HEARTBEAT_INTERVAL = 15.0
LOG_FLUSH_INTERVAL = 10.0
LOG_TEXT_CAP = 200_000  # keep the last ~200KB of a run's logs
WORKER_ID = socket.gethostname()


class _RunLogHandler(logging.Handler):
    """Buffers formatted log records so the worker can persist a run's logs.

    The worker handles one run at a time, so this is attached to the root logger
    for the duration of a single run and detached afterwards. Thread-safe drain
    (logging may emit from worker threads).
    """

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%H:%M:%S")
        )
        self._lock = threading.Lock()
        self._buf: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
        except Exception:  # never let logging crash the run
            return
        with self._lock:
            self._buf.append(line)

    def drain(self) -> str:
        with self._lock:
            if not self._buf:
                return ""
            chunk = "\n".join(self._buf) + "\n"
            self._buf.clear()
            return chunk


async def _flush_logs(run_id: uuid.UUID, handler: "_RunLogHandler", session_factory) -> None:
    """Periodically append the handler's buffered lines to the run's log_text.

    Append + tail-cap happen in one UPDATE via right(coalesce(log_text,'')||chunk,
    cap), so stored logs never exceed LOG_TEXT_CAP. Must never crash the worker.
    """
    while True:
        await asyncio.sleep(LOG_FLUSH_INTERVAL)
        await _flush_once(run_id, handler, session_factory)


async def _flush_once(run_id: uuid.UUID, handler: "_RunLogHandler", session_factory) -> None:
    chunk = handler.drain()
    if not chunk:
        return
    try:
        async with session_factory() as db:
            await db.execute(
                update(ExtractionRun)
                .where(ExtractionRun.id == run_id)
                .values(
                    log_text=func.right(
                        func.coalesce(ExtractionRun.log_text, "") + chunk, LOG_TEXT_CAP
                    )
                )
            )
            await db.commit()
    except Exception:
        logger.exception("Log flush failed for run %s", run_id)


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
        # Capture this run's logs into extraction_runs.log_text for the UI.
        log_handler = _RunLogHandler()
        root_logger = logging.getLogger()
        root_logger.addHandler(log_handler)
        flush = asyncio.create_task(_flush_logs(run_id, log_handler, work_session_factory))
        try:
            async with work_session_factory() as db:
                await firecrawl_service.extract_source(db, source_id, run_id=run_id)
                await db.commit()
        except Exception as exc:
            logger.exception("Run %s failed", run_id)
            # extract_source's own failure handler only flush()es before
            # re-raising, so its FAILED writes roll back when this session exits
            # without committing. Persist the terminal state here on a clean
            # session — both the run AND the source, so the source can't get
            # stuck showing "extracting" after a failed run.
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
                    src = await db.get(DocumentationSource, source_id)
                    if src is not None and src.status == SourceStatus.EXTRACTING:
                        src.status = SourceStatus.FAILED
                        src.error_message = str(exc)[:4096]
                    await db.commit()
        finally:
            hb.cancel()
            flush.cancel()
            root_logger.removeHandler(log_handler)
            # Final flush of anything buffered after the last interval tick.
            await _flush_once(run_id, log_handler, work_session_factory)
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
