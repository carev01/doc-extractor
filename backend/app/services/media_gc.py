"""Media GC — remove media_dir/<article_id>/ directories whose article no longer
exists (orphans left by hard deletes of articles / sources / products / vendors).
Reconciles the media volume against the live articles table, so it catches every
delete path regardless of which route performed it."""
import logging
import os
import shutil
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article import Article

logger = logging.getLogger(__name__)


async def gc_orphaned_media(db: AsyncSession, media_dir: str) -> int:
    """Remove media_dir/<uuid>/ directories with no matching article. Returns the
    number removed. Non-UUID entries are ignored."""
    if not os.path.isdir(media_dir):
        return 0

    candidates: dict[uuid.UUID, str] = {}
    for name in os.listdir(media_dir):
        path = os.path.join(media_dir, name)
        if not os.path.isdir(path):
            continue
        try:
            candidates[uuid.UUID(name)] = path
        except ValueError:
            continue  # not an article-id directory — leave it alone

    if not candidates:
        return 0

    existing = set(
        (await db.execute(
            select(Article.id).where(Article.id.in_(list(candidates)))
        )).scalars()
    )

    removed = 0
    for art_id, path in candidates.items():
        if art_id not in existing:
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
    if removed:
        logger.info("media GC removed %d orphaned image dir(s)", removed)
    return removed
