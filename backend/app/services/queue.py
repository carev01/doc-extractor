"""Postgres-backed extraction job queue (the extraction_runs table)."""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.extraction_run import ExtractionRun, RunStatus


class ActiveRunExists(Exception):
    """Raised when a source already has a pending/running run (coalesce/409)."""


def _is_active_run_violation(exc: IntegrityError) -> bool:
    """Return True only for the uq_active_run_per_source unique-constraint violation."""
    orig = exc.orig
    constraint_match = "uq_active_run_per_source" in str(orig)
    sqlstate_match = getattr(orig, "sqlstate", None) == "23505"
    return constraint_match and sqlstate_match


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
        else:
            run.status = RunStatus.PENDING
            run.claimed_by = None
            run.claimed_at = None
            run.heartbeat_at = None
    await db.commit()
    return len(stale)
