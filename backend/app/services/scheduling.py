"""Scheduler tick: reap dead runs, enqueue due schedules, advance next_run_at."""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.schedule import Schedule
from app.services.cron import compute_next_run
from app.services.export_retention import purge_expired_exports
from app.services.queue import ActiveRunExists, enqueue_run, reap_stale_runs, reap_stale_exports

logger = logging.getLogger(__name__)

# The export retention sweep walks the exports volume, so run it at most hourly
# rather than every tick. Module state resets on scheduler restart (which just
# triggers one sweep after restart — harmless).
_EXPORT_PURGE_INTERVAL = timedelta(hours=1)
_last_export_purge: datetime | None = None


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

    due = (
        await db.execute(
            select(Schedule).where(
                Schedule.enabled.is_(True), Schedule.next_run_at <= now
            )
        )
    ).scalars().all()

    enqueued = 0
    for sched in due:
        # Capture identity before any potential rollback expires the object.
        source_id = sched.source_id
        sched_id = sched.id
        cron = sched.cron
        tz = sched.timezone

        try:
            run = await enqueue_run(db, source_id, trigger="scheduled")
            sched.last_run_at = now
            sched.last_run_id = run.id
            enqueued += 1
        except ActiveRunExists:
            logger.info(
                "Schedule for source %s coalesced — run already active", source_id
            )
            # enqueue_run rolled back the transaction; reload sched so we can
            # still update next_run_at in the same session.
            await db.refresh(sched)

        # Always advance: computing from `now` yields catch-up-once semantics.
        sched.next_run_at = compute_next_run(cron, tz, now)
        await db.commit()

    return {
        "reaped": reaped, "enqueued": enqueued, "due": len(due),
        "reaped_exports": reaped_exports, "purged_exports": purged_exports,
    }
