"""Pydantic schemas for export requests."""

import uuid

from pydantic import BaseModel


class ExportRequest(BaseModel):
    """Request to export documentation as markdown."""

    source_id: uuid.UUID
    # Selection: none = full export
    article_ids: list[uuid.UUID] | None = None  # specific articles
    toc_entry_ids: list[uuid.UUID] | None = None  # specific chapters/sections
    topic_query: str | None = None  # full-text search within content

    # Splitting options
    split_by: str | None = None  # "size" | "articles" | "tokens" | None (no split)
    max_articles_per_file: int | None = None
    max_file_size_bytes: int | None = None
    max_tokens_per_file: int | None = None
    # Align file boundaries to chapters (top-level TOC). Trades uniform file
    # sizes for chapter coherence — files may come out smaller.
    respect_chapters: bool = False


class ExportResponse(BaseModel):
    export_id: uuid.UUID
    source_id: uuid.UUID
    file_count: int
    total_articles: int
    total_size_bytes: int
    zip_filename: str  # self-contained bundle (markdown + images)
    files: list["ExportFileInfo"]


class ExportFileInfo(BaseModel):
    filename: str
    article_count: int
    size_bytes: int
    estimated_tokens: int
    first_article_title: str
    last_article_title: str


class ExtractionTriggerResponse(BaseModel):
    run_id: uuid.UUID
    source_id: uuid.UUID
    status: str
    message: str
