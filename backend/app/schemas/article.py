"""Pydantic schemas for Article."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ArticleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source_id: uuid.UUID
    toc_entry_id: uuid.UUID | None
    title: str
    source_url: str
    last_updated_at: datetime | None
    sort_order: int
    estimated_tokens: int
    content_size_bytes: int
    extracted_at: datetime


class ArticleDetailResponse(ArticleResponse):
    content_markdown: str
    images: list["ArticleImageResponse"] = []


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
