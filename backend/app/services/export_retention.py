"""Export retention — purge old generated export directories so the exports
volume can't fill from unbounded accumulation.

Two policies, applied in order:
  1. Age: terminal (completed/failed/cancelled) export jobs older than
     ``retention_days`` are removed (directory + DB row).
  2. Size cap: if the remaining export footprint still exceeds
     ``max_total_bytes``, evict oldest-first until under cap.

The ``export_jobs`` table is the source of truth; deleting a row and its on-disk
directory together keeps the listing and the filesystem consistent. Orphan
directories (no matching row) older than the age cutoff are swept too.

``batch-<uuid>`` directories — the cached combined zip for a product export —
are the exception that both policies have to be told about. They carry no
``export_jobs`` row of their own, and their name is not a bare UUID, so the
orphan sweep's ``uuid.UUID(entry.name)`` guard skipped them: left alone they were
immortal AND invisible to the size cap, which is how a few hundred MB per product
export would have accumulated forever on a finite volume.

They are also the only thing here that is **regenerable** — a batch zip can be
rebuilt from its children on the next download, whereas evicting a child export
destroys content. So the size cap spends them first, before it evicts any real
export.
"""

import logging
import os
import shutil
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.export_job import ExportJob, ExportStatus

logger = logging.getLogger(__name__)

_TERMINAL = (ExportStatus.COMPLETED, ExportStatus.FAILED, ExportStatus.CANCELLED)


def _dir_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def _remove_export_dir(export_dir: str, export_id: uuid.UUID | None) -> None:
    if export_id is None:
        return
    path = os.path.join(export_dir, str(export_id))
    shutil.rmtree(path, ignore_errors=True)


BATCH_DIR_PREFIX = "batch-"


def _batch_dirs(export_dir: str) -> "list[tuple[str, uuid.UUID, float]]":
    """(path, batch_id, mtime) for every cached product-export zip directory."""
    out = []
    if not os.path.isdir(export_dir):
        return out
    for entry in os.scandir(export_dir):
        if not entry.is_dir() or not entry.name.startswith(BATCH_DIR_PREFIX):
            continue
        try:
            batch_id = uuid.UUID(entry.name[len(BATCH_DIR_PREFIX):])
        except ValueError:
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        out.append((entry.path, batch_id, mtime))
    return out


async def purge_expired_exports(
    db: AsyncSession,
    export_dir: str,
    retention_days: int,
    max_total_bytes: int,
    now: datetime | None = None,
) -> int:
    """Purge export dirs by age then by total-size cap. Returns count removed."""
    now = now or datetime.now(timezone.utc)
    purged = 0

    # 1. Age sweep — terminal jobs past the retention window.
    if retention_days > 0:
        cutoff = now - timedelta(days=retention_days)
        old_jobs = (
            await db.execute(
                select(ExportJob).where(
                    ExportJob.created_at < cutoff,
                    ExportJob.status.in_(_TERMINAL),
                )
            )
        ).scalars().all()
        for job in old_jobs:
            _remove_export_dir(export_dir, job.export_id)
            await db.delete(job)
            purged += 1
        if old_jobs:
            await db.commit()

        # Orphan directories (no matching job row) older than the cutoff.
        if os.path.isdir(export_dir):
            known = {
                str(eid)
                for (eid,) in (
                    await db.execute(select(ExportJob.export_id))
                ).all()
                if eid is not None
            }
            for entry in os.scandir(export_dir):
                if not entry.is_dir() or entry.name in known:
                    continue
                try:
                    uuid.UUID(entry.name)
                except ValueError:
                    continue  # batch-<uuid> and friends; handled below
                if datetime.fromtimestamp(entry.stat().st_mtime, timezone.utc) < cutoff:
                    shutil.rmtree(entry.path, ignore_errors=True)
                    purged += 1

        # Cached product-export zips: stale by age, or dead because every job of
        # the batch has been purged (nothing left to rebuild it from).
        live_batches = {
            bid for (bid,) in (
                await db.execute(
                    select(ExportJob.batch_id).where(ExportJob.batch_id.isnot(None))
                )
            ).all()
        }
        for path, batch_id, mtime in _batch_dirs(export_dir):
            stale = datetime.fromtimestamp(mtime, timezone.utc) < cutoff
            if stale or batch_id not in live_batches:
                shutil.rmtree(path, ignore_errors=True)
                purged += 1

    # 2. Size cap — evict oldest-first until under the cap.
    if max_total_bytes > 0:
        # Cached batch zips count toward the footprint (they are real bytes on the
        # volume) and are spent FIRST: a batch zip rebuilds itself on the next
        # download, so dropping one costs a regeneration, while dropping a child
        # export costs the content.
        batches = sorted(_batch_dirs(export_dir), key=lambda b: b[2])
        batch_sizes = {path: _dir_size(path) for path, _bid, _m in batches}

        completed = (
            await db.execute(
                select(ExportJob)
                .where(
                    ExportJob.status == ExportStatus.COMPLETED,
                    ExportJob.export_id.isnot(None),
                )
                .order_by(ExportJob.created_at)  # oldest first for eviction
            )
        ).scalars().all()
        sizes = {
            job.id: _dir_size(os.path.join(export_dir, str(job.export_id)))
            for job in completed
        }
        total = sum(sizes.values()) + sum(batch_sizes.values())
        for path, _bid, _m in batches:
            if total <= max_total_bytes:
                break
            shutil.rmtree(path, ignore_errors=True)
            total -= batch_sizes[path]
            purged += 1

        evicted = False
        for job in completed:
            if total <= max_total_bytes:
                break
            _remove_export_dir(export_dir, job.export_id)
            await db.delete(job)
            total -= sizes[job.id]
            purged += 1
            evicted = True
        if evicted:
            await db.commit()

    if purged:
        logger.info("Export retention: purged %d export(s)", purged)
    return purged
