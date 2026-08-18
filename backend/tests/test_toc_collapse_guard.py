"""TOC-collapse guard: the pure decision that prevents a degraded run from
wiping a source (see incident 2026-07-12 — an overloaded Firecrawl/Browserless
stack made TOC discovery collapse, and completed runs mass-removed good content).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.firecrawl import _collapse_baseline, _toc_collapsed


def test_collapse_detected_when_toc_far_below_prior():
    # 1 page vs 385 prior (the Index-fallback wipe) → collapse.
    assert _toc_collapsed(1, 385, 0.5, 20) is True
    # Total wipe to 0.
    assert _toc_collapsed(0, 140, 0.5, 20) is True
    # Just under half of prior → collapse.
    assert _toc_collapsed(9, 20, 0.5, 20) is True


def test_no_collapse_for_healthy_or_small_shrink():
    # Same size → fine.
    assert _toc_collapsed(400, 400, 0.5, 20) is False
    # Grew → fine.
    assert _toc_collapsed(500, 400, 0.5, 20) is False
    # Modest shrink above the ratio (e.g. a few pages retired) → fine.
    assert _toc_collapsed(300, 400, 0.5, 20) is False
    # Exactly at the ratio boundary is not "below" → fine.
    assert _toc_collapsed(10, 20, 0.5, 20) is False


def test_small_or_new_sources_never_trip():
    # Below min_prior: a small/new source can legitimately have few pages.
    assert _toc_collapsed(0, 19, 0.5, 20) is False
    assert _toc_collapsed(1, 5, 0.5, 20) is False
    assert _toc_collapsed(0, 0, 0.5, 20) is False


def test_ratio_and_min_prior_are_configurable():
    # Stricter ratio (0.9) trips on a 20% shrink.
    assert _toc_collapsed(80, 100, 0.9, 20) is True
    # Higher min_prior spares a mid-size source.
    assert _toc_collapsed(1, 50, 0.5, 100) is False


def test_baseline_prefers_the_last_successful_run_over_a_duplicated_corpus():
    # Arcserve "Agent for Linux Guide": 518 live articles for 259 distinct URLs
    # (every page stored twice by the pre-#189 raw_http literal-key bug). A healthy
    # 255-page TOC read as < 50% of 518 and every run aborted. The last completed
    # run's own total (259) is the honest baseline, so the run proceeds.
    assert _collapse_baseline(518, 259) == 259
    assert _toc_collapsed(255, _collapse_baseline(518, 259), 0.5, 20) is False
    # Without the fix the same numbers trip the guard — the regression this pins.
    assert _toc_collapsed(255, 518, 0.5, 20) is True


def test_baseline_takes_the_lower_of_the_two_signals():
    # A stale run total must not weaken the guard either: when live is smaller
    # (pages retired since that run), live wins.
    assert _collapse_baseline(120, 400) == 120
    assert _collapse_baseline(400, 120) == 120
    assert _collapse_baseline(300, 300) == 300


def test_baseline_falls_back_to_live_count_without_a_successful_run():
    # First-ever run, or one whose predecessors all failed / were non-extract runs
    # (escalate/enrich leave articles_total at 0).
    assert _collapse_baseline(385, None) == 385
    assert _collapse_baseline(385, 0) == 385


def test_real_collapse_still_trips_on_the_lower_baseline():
    # The protection this guard exists for must survive the baseline change: an
    # empty nav yields 0–1 pages, which is below half of *either* signal.
    assert _toc_collapsed(1, _collapse_baseline(518, 259), 0.5, 20) is True
    assert _toc_collapsed(0, _collapse_baseline(140, 140), 0.5, 20) is True
