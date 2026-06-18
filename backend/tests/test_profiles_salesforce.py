"""Tests for the Salesforce Help extraction profile.

The fixture is a real scraped snapshot of:
  https://help.salesforce.com/s/articleView?id=platform.own_from_salesforce.htm&type=5
captured at ~1.2 MB with waitFor=9000 and a Chrome User-Agent header.

It contains 534 ``<li role="treeitem">`` items, which deduplicate to 480
unique article IDs (54 duplicates, mainly the active article appearing
at the top of the tree).

TOC shape verified from fixture:
  - 480 unique entries
  - Max level: 7 (aria-level 8 → level 7 after 0-basing)
  - Level 0: 1 entry ("Own from Salesforce" — the doc-set root)
"""

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.profiles.salesforce import SalesforceProfile
from app.services.profiles.scraper import FakeScraper

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "platforms")
SALESFORCE_FIXTURE = os.path.join(FIXTURE_DIR, "salesforce.html")

# Other platform fixtures — used to verify detect() returns False
OTHER_FIXTURES = [
    "commvault.html",
    "docusaurus.html",
    "mkdocs.html",
    "gitbook.html",
    "flare_webhelp.html",
    "flare_html5.html",
    "intercom.html",
    "freshdesk.html",
    "confluence.html",
]

# The root URL used with the Salesforce Help fixture
ROOT = "https://help.salesforce.com/s/articleView?id=platform.own_from_salesforce.htm&type=5"

# Expected count of unique entries after article-id deduplication.
# Verified against fixture: 534 raw items, 54 duplicates → 480 unique.
EXPECTED_ENTRY_COUNT = 480


def _read(path: str) -> str:
    return open(path, encoding="utf-8").read()


# ---------------------------------------------------------------------------
# Detection tests
# ---------------------------------------------------------------------------

def test_detect_matches_salesforce():
    """Salesforce fixture must be detected as Salesforce."""
    assert SalesforceProfile().detect(_read(SALESFORCE_FIXTURE), ROOT) is True


@pytest.mark.parametrize("fixture_name", OTHER_FIXTURES)
def test_detect_rejects_other_platforms(fixture_name: str):
    """Salesforce profile must NOT match any of the other platform fixtures."""
    html = _read(os.path.join(FIXTURE_DIR, fixture_name))
    assert SalesforceProfile().detect(html, "https://example.com/") is False, (
        f"SalesforceProfile.detect() incorrectly returned True for {fixture_name}"
    )


# ---------------------------------------------------------------------------
# Content config
# ---------------------------------------------------------------------------

def test_content_config_include_tags():
    cfg = SalesforceProfile().content_config()
    assert cfg.get("includeTags") == [".slds-text-longform"], (
        "content_config must target .slds-text-longform"
    )


def test_content_config_only_main_content_false():
    cfg = SalesforceProfile().content_config()
    assert cfg.get("onlyMainContent") is False


def test_content_config_wait_for():
    cfg = SalesforceProfile().content_config()
    assert cfg.get("waitFor") == 9000, (
        "Salesforce Lightning SPA needs a 9 s waitFor"
    )


# ---------------------------------------------------------------------------
# TOC building
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_build_toc_is_non_empty():
    """build_toc must return at least one entry from the fixture."""
    toc = await SalesforceProfile().build_toc(ROOT, FakeScraper({ROOT: _read(SALESFORCE_FIXTURE)}))
    assert len(toc) > 0, "Expected at least one TOC entry from the Salesforce fixture"


@pytest.mark.asyncio
async def test_build_toc_entry_count_matches_fixture():
    """The fixture yields exactly EXPECTED_ENTRY_COUNT unique entries after dedup."""
    toc = await SalesforceProfile().build_toc(ROOT, FakeScraper({ROOT: _read(SALESFORCE_FIXTURE)}))
    assert len(toc) == EXPECTED_ENTRY_COUNT, (
        f"Expected {EXPECTED_ENTRY_COUNT} entries after dedup, got {len(toc)}"
    )


@pytest.mark.asyncio
async def test_build_toc_contains_known_titles():
    """Spot-check known titles from the fixture."""
    toc = await SalesforceProfile().build_toc(ROOT, FakeScraper({ROOT: _read(SALESFORCE_FIXTURE)}))
    titles = [e.title for e in toc]
    assert "Own from Salesforce" in titles, "'Own from Salesforce' not found"
    assert "Manage API Tokens" in titles, "'Manage API Tokens' not found"


@pytest.mark.asyncio
async def test_build_toc_multiple_distinct_levels():
    """The fixture tree has aria-levels 1–8, so level 0–7 must appear; max >= 2."""
    toc = await SalesforceProfile().build_toc(ROOT, FakeScraper({ROOT: _read(SALESFORCE_FIXTURE)}))
    levels = [e.level for e in toc]
    assert max(levels) >= 2, (
        f"Expected max level >= 2, got {max(levels)}"
    )


@pytest.mark.asyncio
async def test_build_toc_entries_have_absolute_urls():
    """Every entry must have an absolute URL starting with https://."""
    toc = await SalesforceProfile().build_toc(ROOT, FakeScraper({ROOT: _read(SALESFORCE_FIXTURE)}))
    for entry in toc:
        assert entry.url.startswith("https://"), (
            f"Entry URL is not absolute: {entry.url!r}"
        )


@pytest.mark.asyncio
async def test_build_toc_urls_contain_articleview():
    """Every URL must contain 'articleView' — the Salesforce Help URL pattern."""
    toc = await SalesforceProfile().build_toc(ROOT, FakeScraper({ROOT: _read(SALESFORCE_FIXTURE)}))
    for entry in toc:
        assert "articleView" in entry.url, (
            f"URL does not contain articleView: {entry.url!r}"
        )


@pytest.mark.asyncio
async def test_build_toc_no_duplicate_article_ids():
    """After deduplication, no two entries share the same article id (id=KEY param)."""
    toc = await SalesforceProfile().build_toc(ROOT, FakeScraper({ROOT: _read(SALESFORCE_FIXTURE)}))
    ids = []
    for entry in toc:
        m = re.search(r"[?&]id=([^&]+)", entry.url)
        if m:
            ids.append(m.group(1))
    assert len(ids) == len(set(ids)), (
        f"Duplicate article IDs found: {len(ids) - len(set(ids))} duplicates"
    )


@pytest.mark.asyncio
async def test_build_toc_parent_url_is_set_for_children():
    """Child entries (level >= 1) must have a non-None parent_url."""
    toc = await SalesforceProfile().build_toc(ROOT, FakeScraper({ROOT: _read(SALESFORCE_FIXTURE)}))
    children = [e for e in toc if e.level >= 1]
    assert len(children) > 0, "Expected at least one child entry"
    for entry in children:
        assert entry.parent_url is not None, (
            f"Child entry at level {entry.level} has no parent_url: {entry.title!r}"
        )


@pytest.mark.asyncio
async def test_build_toc_root_entry_has_no_parent():
    """The root entry (level 0) must have parent_url=None."""
    toc = await SalesforceProfile().build_toc(ROOT, FakeScraper({ROOT: _read(SALESFORCE_FIXTURE)}))
    roots = [e for e in toc if e.level == 0]
    assert len(roots) >= 1, "Expected at least one level-0 entry"
    for entry in roots:
        assert entry.parent_url is None, (
            f"Root entry should have no parent: {entry.title!r}"
        )


@pytest.mark.asyncio
async def test_build_toc_own_admin_parent_is_root():
    """'Own from Salesforce Administration' (level 1) must have root URL as parent."""
    toc = await SalesforceProfile().build_toc(ROOT, FakeScraper({ROOT: _read(SALESFORCE_FIXTURE)}))
    root_entry = next((e for e in toc if e.title == "Own from Salesforce"), None)
    admin_entry = next((e for e in toc if e.title == "Own from Salesforce Administration"), None)
    assert root_entry is not None, "'Own from Salesforce' not found in TOC"
    assert admin_entry is not None, "'Own from Salesforce Administration' not found in TOC"
    assert admin_entry.parent_url == root_entry.url, (
        f"Expected 'Own from Salesforce Administration' parent to be {root_entry.url!r}, "
        f"got {admin_entry.parent_url!r}"
    )


@pytest.mark.asyncio
async def test_build_toc_empty_scraper_returns_empty():
    """FakeScraper returning empty string → empty TOC."""
    toc = await SalesforceProfile().build_toc(ROOT, FakeScraper({ROOT: ""}))
    assert toc == []


@pytest.mark.asyncio
async def test_build_toc_all_is_article_true():
    """All entries must have is_article=True (the Salesforce tree contains only articles)."""
    toc = await SalesforceProfile().build_toc(ROOT, FakeScraper({ROOT: _read(SALESFORCE_FIXTURE)}))
    for entry in toc:
        assert entry.is_article is True, (
            f"Expected is_article=True for {entry.title!r}"
        )
