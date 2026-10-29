"""Scheduler tick: reap dead runs, reconcile job runs, fan out due jobs."""

import logging
from collections.abc import Sequence
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.sql import any_of
from app.models.auth_realm import AuthRealm, RealmStatus
from app.models.extraction_run import ExtractionRun, RunStatus
from app.models.job import Job
from app.models.job_run import JobRun, JobRunStatus
from app.models.source import DocumentationSource
from app.services.bulk_extract import realm_blocks_extraction
from app.services.cron import compute_next_run
from app.services.job_coverage import resolve_sources
from app.services.queue import reap_stale_exports, reap_stale_runs

logger = logging.getLogger(__name__)


def _needs_human_login(realm: AuthRealm | None) -> bool:
    """True when a run on *realm* could only fail with NeedsLoginError: no saved
    credentials to log in with, and no usable session — expired, or none at all
    (mirrors ``realm_manager.ensure_session``, which accepts only an ACTIVE realm
    with a stored snapshot when there are no credentials)."""
    if realm is None or (realm.username and realm.password):
        return False
    return realm_blocks_extraction(realm) or not (
        realm.status == RealmStatus.ACTIVE and realm.state_snapshot
    )


async def fan_out_job(
    db: AsyncSession, job: Job, trigger: str, now: datetime | None = None
) -> JobRun | None:
    """Create a JobRun and enqueue one pending ExtractionRun per covered source.

    Coverage is the job's resolved sources (tier + vendor/product/source targets).
    A source that cannot be enqueued is recorded in ``job_run.skipped`` with a
    reason: ``paused_run`` (a PAUSED run exists), ``active_run`` (the
    uq_active_run_per_source coalesce, via a savepoint so it never rolls back the
    whole fan-out), ``login_failed`` (realm LOGIN_FAILED) or ``login_needed``
    (no saved credentials and no usable session: expired, or never stored).
    Realms with saved credentials are enqueued; the run logs in at start.
    Returns None when the job covers no sources. Does not commit — the caller does.
    """
    now = now or datetime.now(timezone.utc)
    source_ids = sorted((await resolve_sources(db, [job.id])).get(job.id, set()), key=str)
    if not source_ids:
        return None

    paused = set((
        await db.execute(
            select(ExtractionRun.source_id).where(
                any_of(ExtractionRun.source_id, source_ids),
                ExtractionRun.status == RunStatus.PAUSED,
            )
        )
    ).scalars().all())
    realm_rows = (
        await db.execute(
            select(DocumentationSource.id, AuthRealm)
            .join(AuthRealm, AuthRealm.id == DocumentationSource.auth_realm_id)
            .where(any_of(DocumentationSource.id, source_ids))
        )
    ).all()
    realms = {sid: realm for sid, realm in realm_rows}

    job_run = JobRun(job_id=job.id, trigger=trigger, status=JobRunStatus.PENDING)
    db.add(job_run)
    await db.flush()

    enqueued = 0
    skipped: list[dict] = []
    for sid in source_ids:
        realm = realms.get(sid)
        reason = None
        if sid in paused:
            reason = "paused_run"
        elif realm is not None and realm.status == RealmStatus.LOGIN_FAILED:
            reason = "login_failed"
        elif _needs_human_login(realm):
            reason = "login_needed"
        if reason is None:
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
                reason = "active_run"
            else:
                enqueued += 1
                continue
        logger.info("Job %s: source %s skipped (%s)", job.id, sid, reason)
        skipped.append({"source_id": str(sid), "reason": reason})

    job_run.sources_total = enqueued
    job_run.skipped = skipped or None
    if enqueued == 0:
        # Nothing could be enqueued — nothing to do.
        job_run.status = JobRunStatus.COMPLETED
        job_run.completed_at = now
    job.last_run_at = now
    await db.flush()
    return job_run


async def job_run_children_summary(
    db: AsyncSession, job_run_ids: Sequence[UUID]
) -> dict[UUID, dict]:
    """Per JobRun, child ExtractionRun counts by status (always including
    ``paused``) plus ``total``. One query regardless of how many JobRuns."""
    out = {
        jid: {"total": 0, **{st.value: 0 for st in RunStatus}} for jid in job_run_ids
    }
    if not out:
        return out
    rows = (
        await db.execute(
            select(ExtractionRun.job_run_id, ExtractionRun.status, func.count())
            .where(any_of(ExtractionRun.job_run_id, list(out)))
            .group_by(ExtractionRun.job_run_id, ExtractionRun.status)
        )
    ).all()
    for jid, status, n in rows:
        out[jid][status.value] = n
        out[jid]["total"] += n
    return out


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

    # NOTE: the file-touching sweeps (export-retention purge + orphaned-media GC)
    # run from the worker, not here — the scheduler pod mounts no volumes. See
    # app/services/maintenance.py.

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
        "reaped_exports": reaped_exports,
    }
