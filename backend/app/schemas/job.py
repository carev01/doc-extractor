"""Job / JobRun request/response schemas."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import AliasChoices, BaseModel, Field, field_validator


ScheduleKind = Literal["daily", "weekly", "monthly_day", "monthly_nth"]


class JobScheduleConfig(BaseModel):
    """Schedule fields for a job. Optional — a job can be a manual-only group."""

    enabled: bool = False
    schedule_kind: ScheduleKind | None = None
    time_of_day: str = "02:00"
    timezone: str = "UTC"
    weekday: int | None = None          # 0-6, 0=Sunday (weekly, monthly_nth)
    interval_months: int | None = None  # 1,2,3,4,6,12 (monthly_*)
    start_month: int | None = None      # 1-12
    day_of_month: int | None = None     # 1-28 (monthly_day)
    nth: Literal["first", "last"] | None = None  # monthly_nth
    tier_id: uuid.UUID | None = None


class JobCreate(JobScheduleConfig):
    name: str


class JobUpdate(BaseModel):
    """Partial update — any field omitted is left unchanged."""

    name: str | None = None
    enabled: bool | None = None
    schedule_kind: ScheduleKind | None = None
    time_of_day: str | None = None
    timezone: str | None = None
    weekday: int | None = None
    interval_months: int | None = None
    start_month: int | None = None
    day_of_month: int | None = None
    nth: Literal["first", "last"] | None = None
    tier_id: uuid.UUID | None = None

    @field_validator("name", "enabled")
    @classmethod
    def _not_nullable(cls, v):
        # Omit the field to leave it unchanged; an explicit null would hit the
        # NOT NULL column (a 500). Runs only for fields actually sent.
        if v is None:
            raise ValueError("may not be null")
        return v


class JobTargetsOut(BaseModel):
    vendor_ids: list[uuid.UUID] = []
    product_ids: list[uuid.UUID] = []
    source_ids: list[uuid.UUID] = []


class JobCoverage(BaseModel):
    sources: int = 0
    products: int = 0           # distinct products those sources belong to
    overlap_sources: int = 0    # sources another job also covers
    login_sources: int = 0      # sources behind an auth realm


class CoverageSet(BaseModel):
    source_ids: list[uuid.UUID]


class SchedulePreview(BaseModel):
    """ScheduleSpec fields; ``kind`` may also be sent as ``schedule_kind``."""

    kind: ScheduleKind = Field(validation_alias=AliasChoices("kind", "schedule_kind"))
    time_of_day: str = "02:00"
    timezone: str = "UTC"
    weekday: int | None = None
    interval_months: int | None = None
    start_month: int | None = None
    day_of_month: int | None = None
    nth: Literal["first", "last"] | None = None


class CatalogSource(BaseModel):
    source_id: uuid.UUID
    source_name: str
    auth_realm: str | None = None


class CatalogProduct(BaseModel):
    product_id: uuid.UUID
    product_name: str
    sources: list[CatalogSource] = []


class CatalogVendor(BaseModel):
    vendor_id: uuid.UUID
    vendor_name: str
    tier_id: uuid.UUID | None = None
    products: list[CatalogProduct] = []


class JobResponse(BaseModel):
    id: uuid.UUID
    name: str
    enabled: bool
    schedule_kind: str | None
    time_of_day: str | None
    timezone: str
    weekday: int | None
    interval_months: int | None
    start_month: int | None
    day_of_month: int | None
    nth: str | None
    tier_id: uuid.UUID | None
    cron: str | None
    next_run_at: datetime | None
    last_run_at: datetime | None
    targets: JobTargetsOut = JobTargetsOut()
    coverage: JobCoverage = JobCoverage()
    next_runs: list[datetime] = []


class JobList(BaseModel):
    jobs: list[JobResponse]
    total: int


class JobRunResponse(BaseModel):
    id: uuid.UUID
    job_id: uuid.UUID
    job_name: str | None = None
    status: str
    trigger: str
    sources_total: int
    sources_done: int
    sources_failed: int
    created_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    children_paused: int = 0
    skipped: list[dict] = []
