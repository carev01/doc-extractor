"""Pydantic schemas for Vendor."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class VendorCreate(BaseModel):
    name: str
    website: str | None = None


class VendorUpdate(BaseModel):
    name: str | None = None
    website: str | None = None


class VendorResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    website: str | None
    created_at: datetime
    updated_at: datetime


class VendorListResponse(BaseModel):
    vendors: list[VendorResponse]
    total: int
