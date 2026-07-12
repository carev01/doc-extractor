"""TOC-collapse guard: the pure decision that prevents a degraded run from
wiping a source (see incident 2026-07-12 — an overloaded Firecrawl/Browserless
stack made TOC discovery collapse, and completed runs mass-removed good content).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.firecrawl import _toc_collapsed


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
