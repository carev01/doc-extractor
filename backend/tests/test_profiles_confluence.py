"""Tests for the Confluence extraction profile.

BEST-EFFORT NOTICE
==================
Confluence Cloud's virtualised page-tree sidebar does not render via
Firecrawl (React component — never appears in the scraped HTML).  The
profile therefore collects only the page links that appear inside the
rendered ``.wiki-content`` body of the space overview page.

The fixture used here is a real scraped snapshot of:
  https://documentation.campus.barracuda.com/wiki/spaces/BCCB/overview?homepageId=3244034
captured at ~181 KB with ``waitFor=9000`` and a Chrome User-Agent header.
It contains 22 unique Confluence page links (after deduplication by page ID).

FOLLOW-ON: A full page-tree hierarchy requires the Confluence REST API
(``/wiki/api/v2/spaces/<KEY>/pages``).  That is intentionally out of scope.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.profiles.confluence import ConfluenceProfile
from app.services.profiles.scraper import FakeScraper

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "platforms")

# Confluence fixture
CONFLUENCE_FIXTURE = os.path.join(FIXTURE_DIR, "confluence.html")

# Other platform fixtures — used to verify detect() returns False
OTHER_FIXTURES = [
    "lazy_tree.html",
    "docusaurus.html",
    "mkdocs.html",
    "gitbook.html",
    "flare_webhelp.html",
    "flare_html5.html",
    "intercom.html",
    "freshdesk.html",
]

# The root URL used for the Barracuda Confluence space overview
ROOT = "https://documentation.campus.barracuda.com/wiki/spaces/BCCB/overview?homepageId=3244034"

# Expected count of unique page entries after ID-based deduplication.
# Verified against the live fixture: 25 raw links deduplicate to 22 unique
# page IDs (three bare-ID links are superseded by their title-slug variants).
EXPECTED_PAGE_COUNT = 22


def _read(path: str) -> str:
    return open(path, encoding="utf-8").read()


# ---------------------------------------------------------------------------
# Detection tests
# ---------------------------------------------------------------------------

def test_detect_matches_confluence():
    """Confluence fixture must be detected as Confluence."""
    assert ConfluenceProfile().detect(_read(CONFLUENCE_FIXTURE), ROOT) is True


@pytest.mark.parametrize("fixture_name", OTHER_FIXTURES)
def test_detect_rejects_other_platforms(fixture_name: str):
    """Confluence profile must NOT match any of the 8 other platform fixtures."""
    html = _read(os.path.join(FIXTURE_DIR, fixture_name))
    assert ConfluenceProfile().detect(html, "https://example.com/") is False, (
        f"ConfluenceProfile.detect() incorrectly returned True for {fixture_name}"
    )


def test_detect_rejects_page_that_merely_mentions_atlassian_confluence():
    """A page that only *talks about* Atlassian Confluence (e.g. a changelog for
    a Confluence backup connector) is NOT a Confluence instance — it lacks the
    structural markers. Words alone must not trigger detection."""
    html = (
        "<html><body>"
        "<h3>Confluence backups</h3>"
        "<p>Your backups are fully segregated from Atlassian, so you keep "
        "access to your Confluence data even during an Atlassian outage.</p>"
        "</body></html>"
    )
    assert ConfluenceProfile().detect(html, "https://www.keepit.com/help/product-updates/") is False


# ---------------------------------------------------------------------------
# Content config
# ---------------------------------------------------------------------------

def test_content_config_include_tags():
    cfg = ConfluenceProfile().content_config()
    assert cfg.get("includeTags") == [".wiki-content"], (
        "content_config must target .wiki-content"
    )


def test_content_config_only_main_content_false():
    cfg = ConfluenceProfile().content_config()
    assert cfg.get("onlyMainContent") is False


def test_content_config_wait_for():
    cfg = ConfluenceProfile().content_config()
    assert cfg.get("waitFor") == 9000, "Confluence needs a 9 s waitFor for SPA hydration"


# ---------------------------------------------------------------------------
# TOC building — best-effort, root-only
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_build_toc_is_non_empty():
    """build_toc must return at least one entry from the fixture."""
    toc = await ConfluenceProfile().build_toc(ROOT, FakeScraper({ROOT: _read(CONFLUENCE_FIXTURE)}))
    assert len(toc) > 0, "Expected at least one TOC entry from the Confluence fixture"


@pytest.mark.asyncio
async def test_build_toc_entry_count_matches_fixture():
    """The fixture yields exactly EXPECTED_PAGE_COUNT unique page entries."""
    toc = await ConfluenceProfile().build_toc(ROOT, FakeScraper({ROOT: _read(CONFLUENCE_FIXTURE)}))
    assert len(toc) == EXPECTED_PAGE_COUNT, (
        f"Expected {EXPECTED_PAGE_COUNT} entries after deduplication, got {len(toc)}"
    )


@pytest.mark.asyncio
async def test_build_toc_entries_have_absolute_urls():
    """Every entry must have an absolute URL (starts with https://)."""
    toc = await ConfluenceProfile().build_toc(ROOT, FakeScraper({ROOT: _read(CONFLUENCE_FIXTURE)}))
    for entry in toc:
        assert entry.url.startswith("https://"), (
            f"Entry URL is not absolute: {entry.url!r}"
        )


@pytest.mark.asyncio
async def test_build_toc_entries_have_titles():
    """Every entry must have a non-empty title."""
    toc = await ConfluenceProfile().build_toc(ROOT, FakeScraper({ROOT: _read(CONFLUENCE_FIXTURE)}))
    for entry in toc:
        assert entry.title, f"Entry has empty title: {entry}"


@pytest.mark.asyncio
async def test_build_toc_entries_are_level_zero():
    """All entries from root-only scraping must be at level 0."""
    toc = await ConfluenceProfile().build_toc(ROOT, FakeScraper({ROOT: _read(CONFLUENCE_FIXTURE)}))
    for entry in toc:
        assert entry.level == 0, f"Expected level 0, got {entry.level} for {entry.title!r}"


@pytest.mark.asyncio
async def test_build_toc_urls_contain_page_id():
    """Every entry URL must contain a numeric page ID."""
    import re
    toc = await ConfluenceProfile().build_toc(ROOT, FakeScraper({ROOT: _read(CONFLUENCE_FIXTURE)}))
    for entry in toc:
        assert re.search(r"/pages/\d+", entry.url), (
            f"URL does not contain /pages/<id>: {entry.url!r}"
        )


@pytest.mark.asyncio
async def test_build_toc_no_duplicate_page_ids():
    """After deduplication, no two entries should share the same numeric page ID."""
    import re
    toc = await ConfluenceProfile().build_toc(ROOT, FakeScraper({ROOT: _read(CONFLUENCE_FIXTURE)}))
    page_ids = []
    for entry in toc:
        m = re.search(r"/pages/(\d+)", entry.url)
        if m:
            page_ids.append(m.group(1))
    assert len(page_ids) == len(set(page_ids)), (
        "Duplicate page IDs found in TOC entries"
    )


@pytest.mark.asyncio
async def test_build_toc_prefers_title_slug_url():
    """Where a page appears as both bare-ID and slug form, the slug form wins."""
    toc = await ConfluenceProfile().build_toc(ROOT, FakeScraper({ROOT: _read(CONFLUENCE_FIXTURE)}))
    # These pages appear in both forms in the fixture; the slug form should win.
    slug_urls = [e.url for e in toc if "/Release+Notes" in e.url]
    assert len(slug_urls) >= 1, "Expected at least one entry with title slug in URL"
    bare_urls = [e.url for e in toc if e.url.endswith("/3244108")]
    assert len(bare_urls) == 0, "Bare-ID form /3244108 should have been replaced by slug form"


@pytest.mark.asyncio
async def test_build_toc_empty_scraper_returns_empty():
    """FakeScraper that returns empty string for root URL → empty TOC."""
    toc = await ConfluenceProfile().build_toc(ROOT, FakeScraper({ROOT: ""}))
    assert toc == []


@pytest.mark.asyncio
async def test_build_toc_contains_known_titles():
    """Spot-check a few known page titles from the Barracuda fixture."""
    toc = await ConfluenceProfile().build_toc(ROOT, FakeScraper({ROOT: _read(CONFLUENCE_FIXTURE)}))
    titles = [e.title for e in toc]
    assert "Release Notes" in titles
    assert "Troubleshooting" in titles
    assert "Dashboard" in titles
