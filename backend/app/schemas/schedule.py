"""Schedule request/response schemas."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, field_validator


class ScheduleConfig(BaseModel):
    enabled: bool
    frequency: Literal["hourly", "daily", "weekly", "monthly"]
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


class ScheduleLastRun(BaseModel):
    id: uuid.UUID
    status: str
    completed_at: datetime | None


class ScheduleResponse(BaseModel):
    source_id: uuid.UUID
    enabled: bool
    frequency: str
    time_of_day: str
    day_of_week: int | None
    day_of_month: int | None
    cron: str
    timezone: str
    next_run_at: datetime | None
    last_run_at: datetime | None
    last_run: ScheduleLastRun | None
