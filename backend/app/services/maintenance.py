"""Periodic volume-maintenance sweeps.

These delete files on the media and exports volumes, so they must run where those
volumes are mounted. The scheduler pod mounts no volumes; the worker pod mounts
both — so the worker drives these (see app/worker.py). Each sweep self-gates to
at most hourly via module state, mirroring the previous scheduler hook.
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.services.export_retention import purge_expired_exports
from app.services.media_gc import gc_orphaned_media

logger = logging.getLogger(__name__)

_INTERVAL = timedelta(hours=1)
_last_export_purge: datetime | None = None
_last_media_gc: datetime | None = None


async def run_maintenance_sweeps(
    db: AsyncSession, now: datetime | None = None
) -> dict:
    """Run the export-retention purge and the orphaned-media GC, each gated to at
    most hourly. Returns ``{"purged_exports": int|None, "media_removed": int|None}``;
    a ``None`` value means that sweep was not due on this call.
    """
    now = now or datetime.now(timezone.utc)
    global _last_export_purge, _last_media_gc

    purged_exports: int | None = None
    if _last_export_purge is None or (now - _last_export_purge) >= _INTERVAL:
        purged_exports = await purge_expired_exports(
            db,
            settings.export_dir,
            settings.export_retention_days,
            settings.export_max_total_bytes,
            now=now,
        )
        _last_export_purge = now

    media_removed: int | None = None
    if _last_media_gc is None or (now - _last_media_gc) >= _INTERVAL:
        media_removed = await gc_orphaned_media(db, settings.media_dir)
        _last_media_gc = now

    return {"purged_exports": purged_exports, "media_removed": media_removed}
