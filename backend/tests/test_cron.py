import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.cron import compute_next_run


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
