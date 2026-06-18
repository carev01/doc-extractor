"""Pydantic schemas for Article."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class NamedRef(BaseModel):
    """A lightweight {id, name} reference (vendor / product)."""

    id: uuid.UUID
    name: str


class ChapterRef(BaseModel):
    """A lightweight {id, title} reference to a TOC chapter."""

    id: uuid.UUID
    title: str


class ArticleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source_id: uuid.UUID
    toc_entry_id: uuid.UUID | None
    title: str
    source_url: str
    last_updated_at: datetime | None  # source's own update time, if exposed
    sort_order: int
    estimated_tokens: int
    content_size_bytes: int
    created_at: datetime  # first captured
    extracted_at: datetime  # last scraped


class ArticleDetailResponse(ArticleResponse):
    content_markdown: str
    images: list["ArticleImageResponse"] = []
    # Provenance metadata (derived; the TOC is the source of truth).
    vendor: NamedRef | None = None
    product: NamedRef | None = None
    parent_chapter: ChapterRef | None = None
    top_level_chapter: ChapterRef | None = None


class ArticleImageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    original_url: str
    local_filename: str
    alt_text: str | None
    file_size_bytes: int


class ArticleListResponse(BaseModel):
    articles: list[ArticleResponse]
    total: int


class TOCEntryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    url: str | None
    level: int
    sort_order: int
    is_article: bool
    children: list["TOCEntryResponse"] = []
    article_id: uuid.UUID | None = None


class TOCResponse(BaseModel):
    source_id: uuid.UUID
    entries: list[TOCEntryResponse]
