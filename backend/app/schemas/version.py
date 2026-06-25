"""Pydantic schemas for article version history and changelogs."""

import uuid
from datetime import datetime

from pydantic import BaseModel


class ArticleVersionResponse(BaseModel):
    """A historical snapshot's metadata (no content body)."""

    id: uuid.UUID
    article_id: uuid.UUID
    extraction_run_id: uuid.UUID | None
    content_hash: str | None
    has_diff: bool  # whether a stored diff_text accompanies this version
    content_size_bytes: int
    extracted_at: datetime  # when this content was superseded
    version: str | None = None  # product version of the run that superseded this snapshot


class ArticleVersionDetailResponse(ArticleVersionResponse):
    content_markdown: str


class ArticleVersionListResponse(BaseModel):
    article_id: uuid.UUID
    current_hash: str | None  # hash of the live Article content
    versions: list[ArticleVersionResponse]  # newest-first
    total: int


class VersionDiffResponse(BaseModel):
    article_id: uuid.UUID
    version_id: uuid.UUID
    from_label: str
    to_label: str
    diff_text: str
    computed: bool  # True if generated on the fly, False if stored


class ChangelogEntry(BaseModel):
    # article_id is None for the synthetic "initial" baseline-run summary entry.
    article_id: uuid.UUID | None
    title: str
    change_type: str  # "initial" | "added" | "changed" | "removed"
    timestamp: datetime
    version_id: uuid.UUID | None
    extraction_run_id: uuid.UUID | None
    version: str | None = None  # product version of the entry's extraction run
    has_diff: bool


class ChangelogResponse(BaseModel):
    source_id: uuid.UUID
    entries: list[ChangelogEntry]  # newest-first, across all articles
    total: int
