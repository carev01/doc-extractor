"""Pydantic schemas for DocumentationSource."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models.source import SourceStatus


class SourceCreate(BaseModel):
    vendor_id: uuid.UUID
    name: str
    base_url: str


class SourceUpdate(BaseModel):
    name: str | None = None
    base_url: str | None = None
    platform: str | None = None


class SourceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    vendor_id: uuid.UUID
    name: str
    base_url: str
    status: SourceStatus
    platform: str | None
    last_extracted_at: datetime | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime


class SourceListResponse(BaseModel):
    sources: list[SourceResponse]
    total: int
