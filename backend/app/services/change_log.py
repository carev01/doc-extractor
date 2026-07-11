"""Write helpers for the content_changes outbox + per-run change counts.

record_change / record_removals only ``db.add`` rows to the caller's session;
they do not commit, so the outbox row lands in the same transaction as the
article mutation that triggered it.
"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article import Article
from app.models.content_change import ChangeType, ContentChange


async def record_change(
    db: AsyncSession, *, article: Article, change_type: str, run_id: uuid.UUID
) -> None:
    """Append one added/updated outbox row for *article* (caller commits)."""
    db.add(
        ContentChange(
            article_id=article.id,
            source_id=article.source_id,
            run_id=run_id,
            change_type=change_type,
            content_hash=article.content_hash,
            topic_key=article.topic_key,
        )
    )


async def record_removals(
    db: AsyncSession, *, rows, source_id: uuid.UUID, run_id: uuid.UUID
) -> None:
    """Append a ``removed`` outbox row per element (each has .id, .topic_key)."""
    for r in rows:
        db.add(
            ContentChange(
                article_id=r.id,
                source_id=source_id,
                run_id=run_id,
                change_type=ChangeType.REMOVED.value,
                content_hash=None,
                topic_key=r.topic_key,
            )
        )


async def record_run_start(
    db: AsyncSession, *, source_id: uuid.UUID, run_id: uuid.UUID
) -> None:
    """Append a ``run_start`` sentinel outbox row for a run (caller commits).

    Committed before the run processes any article, this gives the run a visible
    floor in content_changes.id space so the delta feed's safe-ceiling withholds
    the run's later (possibly mid-commit) rows until it finishes — closing the
    flush→commit gap under concurrent multi-replica runs. The feed skips these
    rows (they carry no article and are not a change type it emits)."""
    db.add(
        ContentChange(
            article_id=None,
            source_id=source_id,
            run_id=run_id,
            change_type=ChangeType.RUN_START.value,
            content_hash=None,
            topic_key=None,
        )
    )


async def run_change_counts(db: AsyncSession, run_id: uuid.UUID) -> dict[str, int]:
    """Return {'added': n, 'updated': n, 'removed': n} for a run's outbox rows."""
    stmt = (
        select(ContentChange.change_type, func.count())
        .where(ContentChange.run_id == run_id)
        .group_by(ContentChange.change_type)
    )
    counts = {"added": 0, "updated": 0, "removed": 0}
    for change_type, n in (await db.execute(stmt)).all():
        counts[change_type] = n
    return counts
