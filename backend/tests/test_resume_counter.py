"""Resume bookkeeping: resumed pages count toward 'persisted' for the blocked guard."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.firecrawl import persisted_count


def test_persisted_includes_resumed():
    # extracted, updated, unchanged, resumed
    assert persisted_count(0, 0, 0, 5) == 5


def test_persisted_zero_when_all_zero():
    assert persisted_count(0, 0, 0, 0) == 0


def test_persisted_sums_all_buckets():
    assert persisted_count(2, 3, 10, 5) == 20
