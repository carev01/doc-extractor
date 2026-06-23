"""Job / JobRun request/response schemas."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, field_validator


class JobScheduleConfig(BaseModel):
    """Schedule fields for a job. Optional — a job can be a manual-only group."""

    enabled: bool = False
    frequency: Literal["hourly", "daily", "weekly", "monthly"] | None = None
    time_of_day: str = "02:00"
    day_of_week: int | None = None    # 0-6, 0=Sunday (weekly)
    day_of_month: int | None = None   # 1-28 (monthly)
    timezone: str = "UTC"

    @field_validator("time_of_day")
    @classmethod
    def _valid_time(cls, v: str) -> str:
        hh, mm = v.split(":")
        if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
            raise ValueError("time_of_day must be HH:MM in 24h")
        return v

    @field_validator("day_of_week")
    @classmethod
    def _valid_dow(cls, v: int | None) -> int | None:
        if v is not None and not (0 <= v <= 6):
            raise ValueError("day_of_week must be 0-6")
        return v

    @field_validator("day_of_month")
    @classmethod
    def _valid_dom(cls, v: int | None) -> int | None:
        if v is not None and not (1 <= v <= 28):
            raise ValueError("day_of_month must be 1-28")
        return v


class JobCreate(JobScheduleConfig):
    name: str


class JobUpdate(BaseModel):
    """Partial update — any field omitted is left unchanged."""

    name: str | None = None
    enabled: bool | None = None
    frequency: Literal["hourly", "daily", "weekly", "monthly"] | None = None
    time_of_day: str | None = None
    day_of_week: int | None = None
    day_of_month: int | None = None
    timezone: str | None = None


class JobSourceRef(BaseModel):
    id: uuid.UUID
    name: str
    product_name: str
    vendor_name: str


class JobResponse(BaseModel):
    id: uuid.UUID
    name: str
    enabled: bool
    frequency: str | None
    time_of_day: str | None
    day_of_week: int | None
    day_of_month: int | None
    cron: str | None
    timezone: str
    next_run_at: datetime | None
    last_run_at: datetime | None
    source_count: int
    sources: list[JobSourceRef]


class JobList(BaseModel):
    jobs: list[JobResponse]
    total: int


class JobRunResponse(BaseModel):
    id: uuid.UUID
    job_id: uuid.UUID
    status: str
    trigger: str
    sources_total: int
    sources_done: int
    sources_failed: int
    created_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
