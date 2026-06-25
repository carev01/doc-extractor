"""Pydantic schemas for DocumentationSource."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models.source import SourceStatus


class SourceCreate(BaseModel):
    product_id: uuid.UUID
    name: str
    base_url: str
    url_template: str | None = None


class SourceUpdate(BaseModel):
    name: str | None = None
    base_url: str | None = None
    url_template: str | None = None
    platform: str | None = None
    refresh_profile: bool | None = None
    product_id: uuid.UUID | None = None  # move the source to another product


class SourceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    product_id: uuid.UUID
    job_id: uuid.UUID | None
    name: str
    base_url: str
    url_template: str | None
    status: SourceStatus
    platform: str | None
    last_extracted_at: datetime | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime


class SourceListResponse(BaseModel):
    sources: list[SourceResponse]
    total: int
