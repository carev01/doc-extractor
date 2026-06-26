"""Dashboard response schemas — per-source extraction health."""
import uuid

from pydantic import BaseModel


class DashboardSummary(BaseModel):
    total: int
    never_extracted: int
    stale: int
    failing: int
    running: int


class DashboardSourceRow(BaseModel):
    id: uuid.UUID
    name: str
    vendor_name: str
    product_name: str
    status: str
    last_extracted_at: str | None
    age_seconds: int | None
    article_count: int
    last_run_status: str | None
    last_run_new: int | None
    last_run_updated: int | None
    last_run_unchanged: int | None
    job_id: uuid.UUID | None
    job_name: str | None
    next_run_at: str | None


class DashboardResponse(BaseModel):
    summary: DashboardSummary
    sources: list[DashboardSourceRow]
