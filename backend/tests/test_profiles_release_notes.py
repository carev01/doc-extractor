"""Tests for the release-notes profile (single-page changelog → one document
per feed section).

Some help centers publish a single ``/help/product-updates/`` changelog page
holding several independent feeds in separate sections (e.g. a Platform feed in
``#updates`` and a PMC feed in ``#pmc``). Each feed becomes its own article,
extracted from the same URL via a per-entry ``content_selector``.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.profiles.release_notes import ReleaseNotesProfile
from app.services.profiles.scraper import FakeScraper

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "platforms")
ROOT = "https://www.keepit.com/help/product-updates/"


def _read(name: str) -> str:
    return open(os.path.join(FIXTURE_DIR, name), encoding="utf-8").read()


def _scraper() -> FakeScraper:
    return FakeScraper({ROOT: _read("release_notes.html")})


# ---------------------------------------------------------------------------
# detect()
# ---------------------------------------------------------------------------

def test_detect_true_on_product_updates_url():
    assert ReleaseNotesProfile().detect(_read("release_notes.html"), ROOT) is True


def test_detect_false_on_other_keepit_path():
    # A category page is not the changelog, even though it also references
    # "releasenotes" in shared chrome.
    assert ReleaseNotesProfile().detect(
        _read("category_accordion.html"),
        "https://www.keepit.com/help/microsoft-365-category/",
    ) is False


def test_detect_false_on_foreign_host():
    assert ReleaseNotesProfile().detect(
        _read("release_notes.html"), "https://example.com/help/product-updates/"
    ) is False


# ---------------------------------------------------------------------------
# build_toc()
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_emits_one_article_per_feed():
    toc = await ReleaseNotesProfile().build_toc(ROOT, _scraper())
    assert len(toc) == 2
    assert all(e.is_article and e.level == 0 for e in toc)


@pytest.mark.asyncio
async def test_feed_titles_from_inner_heading_zero_width_stripped():
    toc = await ReleaseNotesProfile().build_toc(ROOT, _scraper())
    titles = [e.title for e in toc]
    assert titles == [
        "What's new in the Keepit Platform",
        "What's new in the Partner Management Console (PMC)",
    ]
    # Zero-width spaces from the source heading must be gone.
    assert "​" not in titles[0]


@pytest.mark.asyncio
async def test_each_feed_has_fragment_url_and_matching_selector():
    toc = await ReleaseNotesProfile().build_toc(ROOT, _scraper())
    by_sel = {e.content_selector: e for e in toc}
    assert set(by_sel) == {"#updates", "#pmc"}
    assert by_sel["#updates"].url == ROOT + "#updates"
    assert by_sel["#pmc"].url == ROOT + "#pmc"


# ---------------------------------------------------------------------------
# render config
# ---------------------------------------------------------------------------

def test_uses_browserless_render_engine():
    assert ReleaseNotesProfile().render_engine == "browserless"


def test_browserless_content_spec_has_no_default_selector():
    # Per-entry content_selector drives extraction; no run-wide selector, and no
    # warm-up navigation is needed (the page isn't WAF-gated).
    spec = ReleaseNotesProfile().browserless_content_spec()
    assert spec.get("selector") is None
    assert spec.get("warmup_url") is None
