"""Friendly-preset -> cron construction and next-run computation."""

from datetime import datetime
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
