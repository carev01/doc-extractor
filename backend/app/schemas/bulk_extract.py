import uuid

from pydantic import BaseModel


class BulkSource(BaseModel):
    source_id: uuid.UUID
    source_name: str
    eligible: bool
    # None when eligible; else not_found | no_write_access | active_run | session_expired
    reason: str | None = None


class BulkProduct(BaseModel):
    product_id: uuid.UUID
    product_name: str
    sources: list[BulkSource]


class BulkPreviewResponse(BaseModel):
    products: list[BulkProduct]


class BulkExtractRequest(BaseModel):
    source_ids: list[uuid.UUID]


class BulkQueued(BaseModel):
    source_id: uuid.UUID
    run_id: uuid.UUID


class BulkSkipped(BaseModel):
    source_id: uuid.UUID
    reason: str


class BulkExtractResponse(BaseModel):
    queued: list[BulkQueued]
    skipped: list[BulkSkipped]
