"""Friendly-preset -> cron construction and next-run computation."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from croniter import croniter

VALID_FREQUENCIES = {"hourly", "daily", "weekly", "monthly"}
_UTC = ZoneInfo("UTC")


def _parse_hh_mm(time_of_day: str) -> tuple[int, int]:
    hour_s, minute_s = time_of_day.split(":")
    return int(hour_s), int(minute_s)


def build_cron(
    frequency: str,
    time_of_day: str = "02:00",
    day_of_week: int | None = None,
    day_of_month: int | None = None,
) -> str:
    """Build a 5-field cron string from friendly preset fields."""
    if frequency not in VALID_FREQUENCIES:
        raise ValueError(f"Unknown frequency: {frequency}")
    hour, minute = _parse_hh_mm(time_of_day)
    if frequency == "hourly":
        return f"{minute} * * * *"
    if frequency == "daily":
        return f"{minute} {hour} * * *"
    if frequency == "weekly":
        dow = 0 if day_of_week is None else day_of_week
        return f"{minute} {hour} * * {dow}"
    # monthly
    dom = 1 if day_of_month is None else day_of_month
    return f"{minute} {hour} {dom} * *"


def compute_next_run(cron: str, timezone: str, after: datetime) -> datetime:
    """Return the next fire time strictly after `after`, as a UTC datetime."""
    tz = ZoneInfo(timezone)
    base = after.astimezone(tz)
    nxt = croniter(cron, base).get_next(datetime)
    return nxt.astimezone(_UTC)


# --- Schedule rules: daily / weekly / every-N-months (day or first/last weekday) ---

VALID_INTERVAL_MONTHS = {1, 2, 3, 4, 6, 12}


@dataclass(frozen=True)
class ScheduleSpec:
    kind: Literal["daily", "weekly", "monthly_day", "monthly_nth"]
    time_of_day: str
    timezone: str = "UTC"
    weekday: int | None = None  # 0 = Sunday
    interval_months: int = 1
    start_month: int | None = None  # 1-12; defaults to January
    day_of_month: int | None = None  # 1-28
    nth: Literal["first", "last"] | None = None


def validate_spec(spec: ScheduleSpec) -> None:
    """Raise ValueError with a human-readable message if the spec is invalid."""
    if spec.kind not in ("daily", "weekly", "monthly_day", "monthly_nth"):
        raise ValueError(f"Unknown schedule kind: {spec.kind}")
    try:
        hour, minute = _parse_hh_mm(spec.time_of_day)
    except (ValueError, AttributeError):
        raise ValueError("Time of day must be HH:MM") from None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("Time of day must be between 00:00 and 23:59")
    try:
        ZoneInfo(spec.timezone)
    except Exception:
        raise ValueError(f"Unknown timezone: {spec.timezone}") from None
    if spec.kind in ("weekly", "monthly_nth"):
        if spec.weekday is None or not 0 <= spec.weekday <= 6:
            raise ValueError("Weekday must be between 0 (Sunday) and 6 (Saturday)")
    if spec.kind in ("monthly_day", "monthly_nth"):
        if spec.interval_months not in VALID_INTERVAL_MONTHS:
            raise ValueError(
                "Month interval must be one of 1, 2, 3, 4, 6 or 12 "
                "(it has to divide a year evenly)"
            )
        if spec.start_month is not None and not 1 <= spec.start_month <= 12:
            raise ValueError("Start month must be between 1 and 12")
    if spec.kind == "monthly_day":
        if spec.day_of_month is None or not 1 <= spec.day_of_month <= 28:
            raise ValueError("Day of month must be between 1 and 28")
    if spec.kind == "monthly_nth" and spec.nth not in ("first", "last"):
        raise ValueError("Specify whether it is the first or the last weekday")


def spec_to_cron(spec: ScheduleSpec) -> str:
    """Translate a validated spec into a 5-field cron string."""
    validate_spec(spec)
    hour, minute = _parse_hh_mm(spec.time_of_day)
    head = f"{minute} {hour}"
    if spec.kind == "daily":
        return f"{head} * * *"
    if spec.kind == "weekly":
        return f"{head} * * {spec.weekday}"
    n = spec.interval_months
    start = spec.start_month or 1
    months = sorted({(start - 1 + k * n) % 12 + 1 for k in range(12 // n)})
    month_field = "*" if n == 1 else ",".join(str(m) for m in months)
    if spec.kind == "monthly_day":
        return f"{head} {spec.day_of_month} {month_field} *"
    dow = f"{spec.weekday}#1" if spec.nth == "first" else f"L{spec.weekday}"
    return f"{head} * {month_field} {dow}"


def next_runs(spec: ScheduleSpec, after: datetime, count: int = 3) -> list[datetime]:
    """Return the next `count` fire times strictly after `after` (aware UTC)."""
    cron = spec_to_cron(spec)
    runs: list[datetime] = []
    cursor = after
    for _ in range(count):
        cursor = compute_next_run(cron, spec.timezone, cursor)
        runs.append(cursor)
    return runs


def job_spec(job) -> ScheduleSpec:
    """Rebuild the ScheduleSpec a Job's stored schedule fields describe."""
    return ScheduleSpec(
        kind=job.schedule_kind,
        time_of_day=job.time_of_day or "02:00",
        timezone=job.timezone or "UTC",
        weekday=job.weekday,
        interval_months=job.interval_months or 1,
        start_month=job.start_month,
        day_of_month=job.day_of_month,
        nth=job.nth,
    )
