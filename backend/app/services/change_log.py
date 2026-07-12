"""Write helpers for the content_changes outbox + per-run change counts.

record_change / record_removals only ``db.add`` rows to the caller's session;
they do not commit, so the outbox row lands in the same transaction as the
article mutation that triggered it.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article import Article
from app.models.content_change import ChangeType, ContentChange

# Transaction-scoped advisory-lock key that serializes out-of-band deletions.
# Their removals are written with run_id=None and no run_start sentinel, so two
# concurrent deletions could otherwise interleave BIGSERIAL ids with an inverted
# commit order; with no active run to set a safe ceiling, the feed would then
# skip the lower id permanently (a lost tombstone). Serializing the deletion
# transactions removes that id/commit-order inversion. The key is an arbitrary
# fixed application constant.
_DELETION_LOCK_KEY = 0x0DE1E7E5


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
    db: AsyncSession, *, rows, source_id: uuid.UUID, run_id: uuid.UUID | None
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


async def record_source_deletions(
    db: AsyncSession, *, source_ids: Sequence[uuid.UUID]
) -> int:
    """Tombstone every live article in *source_ids* (run_id NULL); caller commits.

    Must run BEFORE the sources/articles are deleted, while the articles still
    exist. Returns the number of removed rows written."""
    if not source_ids:
        return 0
    # Serialize concurrent out-of-band deletions so their outbox ids cannot
    # interleave with an inverted commit order (see _DELETION_LOCK_KEY). Held
    # until the caller's transaction ends.
    await db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _DELETION_LOCK_KEY})
    rows = (await db.execute(
        select(Article.id, Article.source_id, Article.topic_key).where(
            Article.source_id.in_(source_ids), Article.removed_at.is_(None)
        )
    )).all()
    for art_id, src_id, topic_key in rows:
        db.add(
            ContentChange(
                article_id=art_id,
                source_id=src_id,
                run_id=None,
                change_type=ChangeType.REMOVED.value,
                content_hash=None,
                topic_key=topic_key,
            )
        )
    return len(rows)


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
        if change_type in counts:
            counts[change_type] = n
    return counts
