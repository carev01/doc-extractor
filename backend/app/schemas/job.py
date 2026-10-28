"""Job / JobRun request/response schemas."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel


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
    skipped: list[dict] | None = None
