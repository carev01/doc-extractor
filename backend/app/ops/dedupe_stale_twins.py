"""One-off: soft-remove the stale literal-key twins kept alive by the reconcile bug.

``_reconcile_removals`` used to link every unlinked article to the TOC entry with
its URL, even one another article already held, so a stale twin — same page,
literal-URL ``topic_key``, created by a bad run and never matched again — was
re-linked next to the real (``{version}``-keyed) article on every run and never
removed. Fixed in the reconcile itself; this clears what it left behind. Measured
2026-10-27: CommCell Console 14,511 twins, Arcserve RHA Administration Guide 356,
each paired one-to-one with its real article, none holding any version history.

A twin is soft-removed exactly as a run would remove a page gone from the TOC
(``removed_at``, unlinked, one ``removed`` outbox row), never deleted, and its
``content_changed_at`` is left alone. Dry run by default: the CLI applies and
rolls back.
"""
import uuid
from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.sql import any_of
from app.models import Article, ExtractionRun
from app.services import change_log
from app.services.delta_feed import _ACTIVE
from app.services.versioning import VERSION_PLACEHOLDER

# CommCell Console, Arcserve RHA Administration Guide.
DEFAULT_SOURCE_IDS = (
    uuid.UUID("b2fab6e5-6229-4e5c-a058-dc48d666aad6"),
    uuid.UUID("0b3a7664-7ff3-4b66-92a7-5393989a37ec"),
)


class DedupeAborted(Exception):
    """A pre- or postcondition failed; nothing may be committed."""


def _twins_query(source_ids: Sequence[uuid.UUID]):
    real = aliased(Article)
    return (
        select(Article.id, Article.source_id, Article.topic_key)
        .join(real, (real.source_id == Article.source_id)
              & (real.source_url == Article.source_url) & (real.id != Article.id))
        .where(
            any_of(Article.source_id, list(source_ids)),
            Article.removed_at.is_(None),
            ~Article.topic_key.contains(VERSION_PLACEHOLDER),
            real.removed_at.is_(None),
            real.topic_key.contains(VERSION_PLACEHOLDER),
        )
        .distinct()
    )


async def dedupe(db: AsyncSession, source_ids: Sequence[uuid.UUID]) -> int:
    """Soft-remove every stale twin in *source_ids*; returns how many. Never commits."""
    await db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": change_log._DELETION_LOCK_KEY})
    active = (await db.execute(
        select(func.count()).select_from(ExtractionRun).where(ExtractionRun.status.in_(_ACTIVE))
    )).scalar_one()
    if active:
        raise DedupeAborted(f"{active} active extraction run(s); refusing to run during a run")

    twins = (await db.execute(_twins_query(source_ids))).all()
    ids = [t.id for t in twins]
    if ids:
        await db.execute(
            update(Article).where(any_of(Article.id, ids))
            .values(removed_at=datetime.now(timezone.utc), removal_run_id=None, toc_entry_id=None)
            .execution_options(synchronize_session=False)
        )
        by_source: dict[uuid.UUID, list] = {}
        for t in twins:
            by_source.setdefault(t.source_id, []).append(t)
        for sid, rows in by_source.items():
            await change_log.record_removals(db, rows=rows, source_id=sid, run_id=None)
        await db.flush()

    left = (await db.execute(select(func.count()).select_from(_twins_query(source_ids).subquery()))).scalar_one()
    if left:
        raise DedupeAborted(f"postcondition: {left} stale twins remain")
    return len(ids)


async def run(db_url: str, source_ids: Sequence[uuid.UUID], apply: bool) -> int:
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    url = make_url(db_url)
    print(f"target: {url.host}:{url.port}/{url.database}  mode: {'APPLY' if apply else 'dry run'}")
    engine = create_async_engine(db_url, poolclass=NullPool)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            try:
                n = await dedupe(db, source_ids)
            except BaseException:
                await db.rollback()
                raise
            await (db.commit() if apply else db.rollback())
            print(f"stale twins soft-removed: {n}", "COMMITTED" if apply else "ROLLED BACK")
            return n
    finally:
        await engine.dispose()


if __name__ == "__main__":
    import argparse
    import asyncio
    import sys

    from app.core.config import settings

    ap = argparse.ArgumentParser(description="Soft-remove stale literal-key twins (one-off).")
    ap.add_argument("--db-url", default=settings.database_url)
    ap.add_argument("--source-id", type=uuid.UUID, action="append")
    ap.add_argument("--apply", action="store_true", help="commit (default: dry run, rolled back)")
    args = ap.parse_args()
    try:
        asyncio.run(run(args.db_url, args.source_id or DEFAULT_SOURCE_IDS, args.apply))
    except DedupeAborted as exc:
        print(f"ABORTED: {exc}", file=sys.stderr)
        sys.exit(1)
