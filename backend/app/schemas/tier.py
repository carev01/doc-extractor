"""Tier request/response schemas."""

import uuid

from pydantic import BaseModel, Field


class TierCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    sort_order: int | None = None


class TierUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    sort_order: int | None = None


class TierResponse(BaseModel):
    id: uuid.UUID
    name: str
    sort_order: int
    vendor_count: int = 0
    job_count: int = 0


class TierVendorsSet(BaseModel):
    vendor_ids: list[uuid.UUID]
