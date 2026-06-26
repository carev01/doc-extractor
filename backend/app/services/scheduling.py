"""Scheduler tick: reap dead runs, reconcile job runs, fan out due jobs."""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.extraction_run import ExtractionRun, RunStatus
from app.models.job import Job
from app.models.job_run import JobRun, JobRunStatus
from app.models.source import DocumentationSource
from app.services.cron import compute_next_run
from app.services.export_retention import purge_expired_exports
from app.services.media_gc import gc_orphaned_media
from app.services.queue import reap_stale_runs, reap_stale_exports

logger = logging.getLogger(__name__)

# The export retention sweep walks the exports volume, so run it at most hourly
# rather than every tick. Module state resets on scheduler restart (which just
# triggers one sweep after restart — harmless).
_EXPORT_PURGE_INTERVAL = timedelta(hours=1)
_last_export_purge: datetime | None = None

# The media GC also walks the media volume, so run it at most hourly.
_MEDIA_GC_INTERVAL = timedelta(hours=1)
_last_media_gc: datetime | None = None


async def fan_out_job(
    db: AsyncSession, job: Job, trigger: str, now: datetime | None = None
) -> JobRun | None:
    """Create a JobRun and enqueue one pending ExtractionRun per assigned source.

    A source that already has an active run is coalesced (skipped) via a
    savepoint so it never rolls back the whole fan-out. Returns the JobRun, or
    None when the job has no sources assigned. Does not commit — the caller does.
    """
    now = now or datetime.now(timezone.utc)
    source_ids = (
        await db.execute(
            select(DocumentationSource.id).where(DocumentationSource.job_id == job.id)
        )
    ).scalars().all()
    if not source_ids:
        return None

    job_run = JobRun(job_id=job.id, trigger=trigger, status=JobRunStatus.PENDING)
    db.add(job_run)
    await db.flush()

    enqueued = 0
    for sid in source_ids:
        try:
            async with db.begin_nested():
                db.add(
                    ExtractionRun(
                        source_id=sid, status=RunStatus.PENDING,
                        trigger=trigger, job_run_id=job_run.id,
                    )
                )
        except IntegrityError:
            # uq_active_run_per_source — a run is already active for this source.
            logger.info("Job %s: source %s coalesced (run already active)", job.id, sid)
        else:
            enqueued += 1

    job_run.sources_total = enqueued
    if enqueued == 0:
        # Every source was already running — nothing to do.
        job_run.status = JobRunStatus.COMPLETED
        job_run.completed_at = now
    job.last_run_at = now
    await db.flush()
    return job_run


async def reconcile_job_runs(db: AsyncSession, now: datetime | None = None) -> int:
    """Advance open JobRuns from the aggregate state of their child runs."""
    now = now or datetime.now(timezone.utc)
    open_runs = (
        await db.execute(
            select(JobRun).where(
                JobRun.status.in_([JobRunStatus.PENDING, JobRunStatus.RUNNING])
            )
        )
    ).scalars().all()

    changed = 0
    for jr in open_runs:
        children = (
            await db.execute(
                select(ExtractionRun.status).where(ExtractionRun.job_run_id == jr.id)
            )
        ).scalars().all()
        if not children:
            continue  # fan_out handles the 0-child (all-coalesced) case

        completed = sum(1 for c in children if c == RunStatus.COMPLETED)
        failed = sum(1 for c in children if c == RunStatus.FAILED)
        cancelled = sum(1 for c in children if c == RunStatus.CANCELLED)
        terminal = completed + failed + cancelled
        new_status = jr.status

        if terminal < len(children):
            # Still in progress. RUNNING once anything has left PENDING.
            if any(c != RunStatus.PENDING for c in children):
                new_status = JobRunStatus.RUNNING
                if jr.started_at is None:
                    jr.started_at = now
        else:
            if completed == len(children):
                new_status = JobRunStatus.COMPLETED
            elif failed == len(children):
                new_status = JobRunStatus.FAILED
            elif cancelled == len(children):
                new_status = JobRunStatus.CANCELLED
            else:
                new_status = JobRunStatus.PARTIAL
            jr.completed_at = now
            jr.sources_done = completed
            jr.sources_failed = failed

        if new_status != jr.status:
            jr.status = new_status
            changed += 1

    await db.commit()
    return changed


async def tick(db: AsyncSession, now: datetime | None = None) -> dict:
    """One scheduler iteration. Idempotent and safe to call repeatedly."""
    now = now or datetime.now(timezone.utc)
    reaped = await reap_stale_runs(db)
    reaped_exports = await reap_stale_exports(db)

    global _last_export_purge
    purged_exports = 0
    if _last_export_purge is None or (now - _last_export_purge) >= _EXPORT_PURGE_INTERVAL:
        purged_exports = await purge_expired_exports(
            db,
            settings.export_dir,
            settings.export_retention_days,
            settings.export_max_total_bytes,
            now=now,
        )
        _last_export_purge = now

    global _last_media_gc
    if _last_media_gc is None or (now - _last_media_gc) >= _MEDIA_GC_INTERVAL:
        await gc_orphaned_media(db, settings.media_dir)
        _last_media_gc = now

    reconciled = await reconcile_job_runs(db, now)

    due = (
        await db.execute(
            select(Job).where(Job.enabled.is_(True), Job.next_run_at <= now)
        )
    ).scalars().all()

    enqueued = 0
    for job in due:
        cron = job.cron
        tz = job.timezone
        job_run = await fan_out_job(db, job, trigger="scheduled", now=now)
        # Always advance: computing from `now` yields catch-up-once semantics.
        job.next_run_at = compute_next_run(cron, tz, now) if cron else None
        await db.commit()
        if job_run is not None and job_run.sources_total > 0:
            enqueued += 1

    return {
        "reaped": reaped, "enqueued": enqueued, "due": len(due),
        "reconciled": reconciled,
        "reaped_exports": reaped_exports, "purged_exports": purged_exports,
    }
