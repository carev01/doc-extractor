"""Webhook request/response schemas."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, HttpUrl


EVENT_TYPES = Literal["new_page", "updated_page", "removed_page", "extraction_complete"]


class WebhookCreate(BaseModel):
    url: HttpUrl
    label: str | None = None
    events: list[EVENT_TYPES] = ["extraction_complete"]
    secret: str | None = None
    source_id: uuid.UUID | None = None
    is_active: bool = True


class WebhookUpdate(BaseModel):
    url: HttpUrl | None = None
    label: str | None = None
    events: list[EVENT_TYPES] | None = None
    secret: str | None = None
    is_active: bool | None = None
    source_id: uuid.UUID | None = None


class WebhookResponse(BaseModel):
    id: uuid.UUID
    source_id: uuid.UUID | None
    url: str
    label: str | None
    events: list[str]
    secret: str | None
    is_active: bool
    last_status_code: int | None
    last_attempt_at: datetime | None
    last_error: str | None
    total_deliveries: int
    total_failures: int
    created_at: datetime
    updated_at: datetime


class WebhookListResponse(BaseModel):
    webhooks: list[WebhookResponse]
    total: int


class WebhookDeliveryResponse(BaseModel):
    id: uuid.UUID
    webhook_id: uuid.UUID
    event_type: str
    run_id: uuid.UUID | None
    source_id: uuid.UUID | None
    status_code: int | None
    error: str | None
    attempt: int
    success: bool
    created_at: datetime


class WebhookDeliveryListResponse(BaseModel):
    deliveries: list[WebhookDeliveryResponse]
    total: int


class WebhookTestResponse(BaseModel):
    success: bool
    status_code: int | None
    error: str | None