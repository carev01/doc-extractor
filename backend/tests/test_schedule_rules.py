from datetime import datetime, timezone

import pytest

from app.services.cron import ScheduleSpec, next_runs, spec_to_cron, validate_spec

AFTER = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)


def _dates(spec, after=AFTER):
    return [r.date().isoformat() for r in next_runs(spec, after)]


def test_every_month_first_sunday():
    spec = ScheduleSpec(kind="monthly_nth", time_of_day="02:00", weekday=0, nth="first")
    assert spec_to_cron(spec) == "0 2 * * 0#1"
    assert _dates(spec) == ["2026-11-01", "2026-12-06", "2027-01-03"]


def test_every_3_months_last_saturday_from_january():
    spec = ScheduleSpec(
        kind="monthly_nth", time_of_day="02:00", weekday=6, nth="last",
        interval_months=3, start_month=1,
    )
    assert spec_to_cron(spec) == "0 2 * 1,4,7,10 L6"
    assert _dates(spec, datetime(2026, 11, 1, tzinfo=timezone.utc)) == [
        "2027-01-30", "2027-04-24", "2027-07-31",
    ]


def test_every_2_months_on_the_3rd_from_november():
    spec = ScheduleSpec(
        kind="monthly_day", time_of_day="02:00", day_of_month=3,
        interval_months=2, start_month=11,
    )
    assert spec_to_cron(spec) == "0 2 3 1,3,5,7,9,11 *"
    assert _dates(spec) == ["2026-11-03", "2027-01-03", "2027-03-03"]


def test_weekly_and_daily():
    daily = ScheduleSpec(kind="daily", time_of_day="06:30")
    assert spec_to_cron(daily) == "30 6 * * *"
    assert _dates(daily) == ["2026-10-29", "2026-10-30", "2026-10-31"]
    weekly = ScheduleSpec(kind="weekly", time_of_day="02:00", weekday=1)
    assert spec_to_cron(weekly) == "0 2 * * 1"
    assert _dates(weekly) == ["2026-11-02", "2026-11-09", "2026-11-16"]


def test_timezone_is_respected():
    spec = ScheduleSpec(kind="daily", time_of_day="02:00", timezone="Europe/Lisbon")
    runs = next_runs(spec, datetime(2026, 7, 1, tzinfo=timezone.utc), count=1)
    assert runs[0].hour == 1 and runs[0].utcoffset().total_seconds() == 0
    winter = next_runs(spec, datetime(2026, 12, 1, tzinfo=timezone.utc), count=1)
    assert winter[0].hour == 2


def test_interval_must_divide_12():
    spec = ScheduleSpec(
        kind="monthly_day", time_of_day="02:00", day_of_month=1,
        interval_months=5, start_month=1,
    )
    with pytest.raises(ValueError, match="interval"):
        validate_spec(spec)


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(kind="monthly_day", day_of_month=29),
        dict(kind="weekly", weekday=7),
        dict(kind="monthly_nth", weekday=0),
        dict(kind="hourly"),
        dict(kind="daily", time_of_day="25:00"),
    ],
)
def test_field_ranges(kwargs):
    kwargs.setdefault("time_of_day", "02:00")
    with pytest.raises(ValueError):
        validate_spec(ScheduleSpec(**kwargs))
