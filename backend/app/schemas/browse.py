"""Schemas for the documentation browser — annotated TOC + removed pages."""

import uuid
from datetime import datetime

from pydantic import BaseModel


class BrowseTOCEntry(BaseModel):
    """A TOC node, annotated with per-article change status when it's a page."""

    id: uuid.UUID
    title: str
    url: str | None
    level: int
    sort_order: int
    is_article: bool
    article_id: uuid.UUID | None = None
    # None for non-article (section) nodes; otherwise new|updated|unchanged.
    change_status: str | None = None
    version_count: int = 0
    last_updated_at: datetime | None = None
    children: list["BrowseTOCEntry"] = []


class RemovedArticle(BaseModel):
    """A previously-extracted article no longer present in the current TOC."""

    article_id: uuid.UUID
    title: str
    source_url: str
    last_extracted_at: datetime
    version_count: int


class BrowseResponse(BaseModel):
    source_id: uuid.UUID
    latest_run_id: uuid.UUID | None  # run the change-status is relative to
    entries: list[BrowseTOCEntry]
    removed: list[RemovedArticle]
