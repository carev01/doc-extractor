"""Persistent checkpoint for a long TOC build, so it resumes after interruption.

Used by profiles whose sidebar is expanded section-by-section (Commvault). Each
completed section is committed on its **own** short-lived session, independent of
the run's main work transaction — so progress survives even if that transaction
later rolls back or the worker dies. Keyed by source (one active run per source).
"""

import logging
import uuid

from sqlalchemy import delete, select

from app.models.toc_checkpoint import TocCheckpoint

logger = logging.getLogger(__name__)


class TocBuildCheckpoint:
    """Read/modify/write of a source's TOC-build checkpoint row.

    ``session_factory`` is an async_sessionmaker; each method opens, commits, and
    closes its own session.
    """

    def __init__(self, session_factory, source_id: uuid.UUID):
        self._sf = session_factory
        self.source_id = source_id

    async def load(self) -> dict:
        """Return saved {"top_level": [...], "sections": {id: [...]}} or {}."""
        async with self._sf() as db:
            row = await db.get(TocCheckpoint, self.source_id)
            return dict(row.data) if row and row.data else {}

    async def save_top_level(self, tops: list) -> None:
        await self._merge({"top_level": tops})

    async def save_data(self, patch: dict) -> None:
        """Shallow-merge arbitrary keys into the checkpoint blob (used by profiles
        with their own resume shape, e.g. GitBook's crawl state)."""
        await self._merge(patch)

    async def save_section(self, section_id: str, nodes: list) -> None:
        async with self._sf() as db:
            row = await db.get(TocCheckpoint, self.source_id)
            data = dict(row.data) if row and row.data else {}
            sections = dict(data.get("sections") or {})
            sections[section_id] = nodes
            data["sections"] = sections
            await self._write(db, row, data)
            await db.commit()

    async def load_content_done(self) -> set[str]:
        """URLs whose content was already scraped in this extraction cycle."""
        data = await self.load()
        return set(data.get("content_done") or [])

    async def add_content_done(self, urls: list[str]) -> None:
        """Mark a chunk of URLs as scraped (committed immediately so a later
        failure/restart resumes from here)."""
        if not urls:
            return
        async with self._sf() as db:
            row = await db.get(TocCheckpoint, self.source_id)
            data = dict(row.data) if row and row.data else {}
            done = list(data.get("content_done") or [])
            done.extend(urls)
            data["content_done"] = done
            await self._write(db, row, data)
            await db.commit()

    async def clear(self) -> None:
        async with self._sf() as db:
            await db.execute(
                delete(TocCheckpoint).where(TocCheckpoint.source_id == self.source_id)
            )
            await db.commit()

    async def _merge(self, patch: dict) -> None:
        async with self._sf() as db:
            row = await db.get(TocCheckpoint, self.source_id)
            data = dict(row.data) if row and row.data else {}
            data.update(patch)
            await self._write(db, row, data)
            await db.commit()

    async def _write(self, db, row, data: dict) -> None:
        # Reassign the JSONB value (don't mutate in place) so SQLAlchemy flushes it.
        if row is None:
            db.add(TocCheckpoint(source_id=self.source_id, data=data))
        else:
            row.data = data
