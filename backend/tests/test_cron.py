import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.cron import build_cron, compute_next_run


def test_build_cron_daily():
    assert build_cron("daily", "02:30") == "30 2 * * *"


def test_build_cron_hourly_uses_minute():
    assert build_cron("hourly", "00:15") == "15 * * * *"


def test_build_cron_weekly_includes_dow():
    assert build_cron("weekly", "02:00", day_of_week=0) == "0 2 * * 0"


def test_build_cron_monthly_includes_dom():
    assert build_cron("monthly", "02:00", day_of_month=1) == "0 2 1 * *"


def test_build_cron_rejects_unknown_frequency():
    with pytest.raises(ValueError):
        build_cron("yearly", "02:00")


def test_compute_next_run_is_utc_and_in_future():
    after = datetime(2026, 6, 17, 0, 0, tzinfo=timezone.utc)
    nxt = compute_next_run("0 2 * * *", "UTC", after)
    assert nxt == datetime(2026, 6, 17, 2, 0, tzinfo=timezone.utc)


def test_compute_next_run_respects_timezone():
    # Lisbon is UTC+1 in June (DST); 02:00 local == 01:00 UTC.
    after = datetime(2026, 6, 17, 0, 0, tzinfo=timezone.utc)
    nxt = compute_next_run("0 2 * * *", "Europe/Lisbon", after)
    assert nxt == datetime(2026, 6, 17, 1, 0, tzinfo=timezone.utc)


def test_compute_next_run_catches_up_once_when_overdue():
    # 'after' is past today's 02:00 fire -> next fire is tomorrow, computed once.
    after = datetime(2026, 6, 17, 4, 30, tzinfo=timezone.utc)
    nxt = compute_next_run("0 2 * * *", "UTC", after)
    assert nxt == datetime(2026, 6, 18, 2, 0, tzinfo=timezone.utc)
