"""Media volume reconciliation against the DB.

gc_orphaned_media    — remove media_dir/<article_id>/ dirs whose article is gone
                       (orphans left by hard deletes of articles / sources /
                       products / vendors), catching every delete path regardless
                       of which route performed it.
backfill_image_sizes — fill ArticleImage.file_size_bytes rows left at the default 0.

Both read or write the media volume, so they run from the worker's maintenance
sweeps (the only pod that mounts it)."""
import logging
import os
import shutil
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.sql import any_of
from app.models.article import Article
from app.models.image import ArticleImage

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

    # One array parameter, not one per id: there is a media dir per article, so an
    # IN list here scales with the whole corpus and blows asyncpg's 32767
    # bind-parameter cap (which it did — every sweep raised InterfaceError, so no
    # media was ever collected). See app/core/sql.any_of.
    existing = set(
        (await db.execute(
            select(Article.id).where(any_of(Article.id, candidates))
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


async def backfill_image_sizes(
    db: AsyncSession, media_dir: str, *, batch: int = 1000
) -> int:
    """Fill ``ArticleImage.file_size_bytes`` for rows stored with the column default.

    Only the PDF path ever set the size; both web scrape paths left it at 0, so the
    article API reported every web-sourced image as zero bytes (80,393 of 87,243
    rows in production). The write sites now set it — this repairs what they already
    wrote, reading each size off the media volume.

    Converges: a filled row stops matching. A row whose file is missing keeps its 0
    rather than being given a fabricated size, so it is re-checked next sweep — a
    small, cheap residue, and an honest "unknown". Paged by primary key so that
    residue can't make the loop spin on the same batch forever.
    """
    if not os.path.isdir(media_dir):
        return 0

    filled = 0
    cursor = uuid.UUID(int=0)
    while True:
        rows = (await db.execute(
            select(ArticleImage)
            .where(ArticleImage.file_size_bytes == 0, ArticleImage.id > cursor)
            .order_by(ArticleImage.id)
            .limit(batch)
        )).scalars().all()
        if not rows:
            break
        cursor = rows[-1].id
        for img in rows:
            path = os.path.join(media_dir, str(img.article_id), img.local_filename)
            try:
                size = os.path.getsize(path)
            except OSError:
                continue  # file gone — leave the 0, don't invent a size
            if size > 0:
                img.file_size_bytes = size
                filled += 1
        await db.commit()

    if filled:
        logger.info("backfilled file_size_bytes for %d image row(s)", filled)
    return filled
