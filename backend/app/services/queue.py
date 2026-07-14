"""Postgres-backed extraction job queue (the extraction_runs table)."""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.extraction_run import ExtractionRun, RunStatus
from app.models.export_job import ExportJob, ExportStatus
from app.models.source import DocumentationSource, SourceStatus


class ActiveRunExists(Exception):
    """Raised when a source already has a pending/running run (coalesce/409)."""


def retry_delay_seconds(attempts: int) -> int:
    """Exponential backoff for a requeued run: base * 2^(attempts-1), capped.

    ``attempts`` is the run's attempt count (already incremented for the try that
    just failed), so the first retry waits ``base`` seconds. Pure/synchronous so
    the schedule is unit-testable.
    """
    base = settings.pdf_download_retry_base_seconds
    cap = settings.pdf_download_retry_max_seconds
    exp = max(0, attempts - 1)
    # Clamp the shift before multiplying so a large attempts count can't overflow.
    delay = base * (2 ** min(exp, 20))
    return int(min(delay, cap))


def _is_active_run_violation(exc: IntegrityError) -> bool:
    """Return True only for the uq_active_run_per_source unique-constraint violation."""
    orig = exc.orig
    constraint_match = "uq_active_run_per_source" in str(orig)
    sqlstate_match = getattr(orig, "sqlstate", None) == "23505"
    return constraint_match and sqlstate_match


async def enqueue_run(
    db: AsyncSession, source_id: uuid.UUID, trigger: str = "manual",
    kind: str = "extract",
) -> ExtractionRun:
    """Insert a pending run. Raises ActiveRunExists if one is already active."""
    run = ExtractionRun(
        source_id=source_id, status=RunStatus.PENDING, trigger=trigger, kind=kind,
    )
    db.add(run)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        if _is_active_run_violation(exc):
            raise ActiveRunExists(str(source_id)) from exc
        raise
    await db.refresh(run)
    return run


async def claim_next_run(
    db: AsyncSession, worker_id: str
) -> ExtractionRun | None:
    """Atomically claim the oldest *ready* pending run, or None if none is ready.

    A run requeued with backoff (next_attempt_at in the future) is skipped until
    its delay elapses, so a transiently-failing source doesn't block the queue.
    Ready runs are ordered by coalesce(next_attempt_at, created_at): fresh runs
    keep FIFO order, and a requeued run sorts to the back (its next_attempt_at is
    later than the others' created_at).
    """
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(ExtractionRun)
        .where(
            ExtractionRun.status == RunStatus.PENDING,
            or_(
                ExtractionRun.next_attempt_at.is_(None),
                ExtractionRun.next_attempt_at <= now,
            ),
        )
        .order_by(func.coalesce(ExtractionRun.next_attempt_at, ExtractionRun.created_at))
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
    run.next_attempt_at = None
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
        select(ExtractionRun)
        .where(
            ExtractionRun.status == RunStatus.RUNNING,
            or_(ExtractionRun.heartbeat_at.is_(None), ExtractionRun.heartbeat_at < cutoff),
        )
        .with_for_update(skip_locked=True)
    )
    stale = result.scalars().all()
    for run in stale:
        if run.attempts >= max_attempts:
            run.status = RunStatus.FAILED
            run.error_message = (run.error_message or "worker lost")[:4096]
            run.completed_at = datetime.now(timezone.utc)
            # Don't leave the source stuck at "extracting" once the run is dead.
            src = await db.get(DocumentationSource, run.source_id)
            if src is not None and src.status == SourceStatus.EXTRACTING:
                src.status = SourceStatus.FAILED
                src.error_message = (run.error_message or "worker lost")[:4096]
        else:
            run.status = RunStatus.PENDING
            run.claimed_by = None
            run.claimed_at = None
            run.heartbeat_at = None
    await db.commit()
    return len(stale)


async def enqueue_export(
    db: AsyncSession, source_id: uuid.UUID, request: dict
) -> ExportJob:
    """Insert a pending export job."""
    job = ExportJob(source_id=source_id, request=request, status=ExportStatus.PENDING)
    db.add(job)
    await db.commit()
    await db.refresh(job)
    return job


async def claim_next_export(
    db: AsyncSession, worker_id: str
) -> ExportJob | None:
    """Atomically claim the oldest pending export job, or None if empty."""
    result = await db.execute(
        select(ExportJob)
        .where(ExportJob.status == ExportStatus.PENDING)
        .order_by(ExportJob.created_at)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    job = result.scalar_one_or_none()
    if job is None:
        return None
    now = datetime.now(timezone.utc)
    job.status = ExportStatus.RUNNING
    job.claimed_by = worker_id
    job.claimed_at = now
    job.heartbeat_at = now
    job.started_at = now
    job.attempts += 1
    await db.commit()
    await db.refresh(job)
    return job


async def reap_stale_exports(
    db: AsyncSession, max_attempts: int = 3, stale_seconds: int = 300
) -> int:
    """Requeue (or fail, at the attempt cap) export jobs whose worker stopped heartbeating."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_seconds)
    result = await db.execute(
        select(ExportJob)
        .where(
            ExportJob.status == ExportStatus.RUNNING,
            or_(ExportJob.heartbeat_at.is_(None), ExportJob.heartbeat_at < cutoff),
        )
        .with_for_update(skip_locked=True)
    )
    stale = result.scalars().all()
    for job in stale:
        if job.attempts >= max_attempts:
            job.status = ExportStatus.FAILED
            job.error_message = (job.error_message or "worker lost")[:4096]
            job.completed_at = datetime.now(timezone.utc)
        else:
            job.status = ExportStatus.PENDING
            job.claimed_by = None
            job.claimed_at = None
            job.heartbeat_at = None
    await db.commit()
    return len(stale)
