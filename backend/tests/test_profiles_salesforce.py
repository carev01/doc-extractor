"""Tests for the Salesforce Help extraction profile.

Salesforce Help renders its nav tree and article body inside shadow DOM
(Lightning Web Components). The newer "xcloud" tree is also lazy (children mount
on toggle click) and no longer carries ``role="treeitem"``, so the profile builds
its TOC from ``scraper.expand_salesforce_tree`` — a depth-first Browserless
expansion that returns ``{title, href, level}`` (0-based level) scoped to the
subtree rooted at the source URL. detect() still keys off the raw page markers.
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

OTHER_FIXTURES = [
    "lazy_tree.html", "docusaurus.html", "mkdocs.html", "gitbook.html",
    "flare_webhelp.html", "flare_html5.html", "intercom.html", "freshdesk.html",
    "confluence.html",
]

ANCHOR_ID = "platform.own_from_salesforce.htm"
ROOT = f"https://help.salesforce.com/s/articleView?id={ANCHOR_ID}&type=5"


def _av(article_id: str) -> str:
    return f"articleView?id={article_id}&type=5"


# Canned Browserless subtree-expansion result mirroring the real xcloud tree:
# the anchor page at depth 0 (repeated once at the top — must dedup), then nested
# descendants at deeper depths. Levels are already 0-based (relative to anchor).
TREE = [
    {"title": "Own from Salesforce", "href": _av("platform.own_from_salesforce.htm"), "level": 0},
    # Anchor repeats at the top of its own subtree — must dedup away.
    {"title": "Own from Salesforce", "href": _av("platform.own_from_salesforce.htm"), "level": 0},
    {"title": "Own from Salesforce Administration", "href": _av("platform.own_admin.htm"), "level": 1},
    {"title": "Manage API Tokens", "href": _av("platform.own_api_tokens.htm"), "level": 2},
    {"title": "Backups", "href": _av("platform.own_backups.htm"), "level": 1},
]
EXPECTED_ENTRY_COUNT = 4  # 5 raw items, 1 duplicate


def _read(path: str) -> str:
    return open(path, encoding="utf-8").read()


def _scraper():
    # Keyed by anchor id: proves the profile derives the anchor from the URL and
    # scopes the expansion to that subtree.
    return FakeScraper({}, salesforce_tree_by_url={ANCHOR_ID: TREE})


# ── Detection ──────────────────────────────────────────────────────────────

def test_detect_matches_salesforce():
    assert SalesforceProfile().detect(_read(SALESFORCE_FIXTURE), ROOT) is True


@pytest.mark.parametrize("fixture_name", OTHER_FIXTURES)
def test_detect_rejects_other_platforms(fixture_name: str):
    html = _read(os.path.join(FIXTURE_DIR, fixture_name))
    assert SalesforceProfile().detect(html, "https://example.com/") is False


# ── Content config ───────────────────────────────────────────────────────────

def test_content_config_include_tags():
    assert SalesforceProfile().content_config().get("includeTags") == [".slds-text-longform"]


def test_content_config_only_main_content_false():
    assert SalesforceProfile().content_config().get("onlyMainContent") is False


def test_content_config_wait_for():
    assert SalesforceProfile().content_config().get("waitFor") == 9000


def test_uses_browserless_render_engine():
    assert SalesforceProfile().render_engine == "browserless"


# ── TOC building (from Browserless subtree expansion) ────────────────────────

@pytest.mark.asyncio
async def test_build_toc_dedup_and_count():
    toc = await SalesforceProfile().build_toc(ROOT, _scraper())
    assert len(toc) == EXPECTED_ENTRY_COUNT


@pytest.mark.asyncio
async def test_build_toc_levels_and_titles():
    toc = await SalesforceProfile().build_toc(ROOT, _scraper())
    got = [(e.title, e.level) for e in toc]
    assert got == [
        ("Own from Salesforce", 0),                 # anchor at depth 0
        ("Own from Salesforce Administration", 1),
        ("Manage API Tokens", 2),
        ("Backups", 1),
    ]


@pytest.mark.asyncio
async def test_build_toc_parent_linkage():
    toc = await SalesforceProfile().build_toc(ROOT, _scraper())
    by_title = {e.title: e for e in toc}
    assert by_title["Own from Salesforce"].parent_url is None
    assert by_title["Own from Salesforce Administration"].parent_url == by_title["Own from Salesforce"].url
    # Deep child hangs off the nearest entry one level up, not the root.
    assert by_title["Manage API Tokens"].parent_url == by_title["Own from Salesforce Administration"].url
    # 'Backups' (back at level 1) re-parents to the root, not the deeper API node.
    assert by_title["Backups"].parent_url == by_title["Own from Salesforce"].url


@pytest.mark.asyncio
async def test_build_toc_absolute_articleview_urls_no_dup_ids():
    toc = await SalesforceProfile().build_toc(ROOT, _scraper())
    ids = []
    for e in toc:
        assert e.url.startswith("https://") and "articleView" in e.url
        assert e.is_article is True
        m = re.search(r"[?&]id=([^&]+)", e.url)
        if m:
            ids.append(m.group(1))
    assert len(ids) == len(set(ids))


@pytest.mark.asyncio
async def test_build_toc_scopes_to_subtree_anchor():
    # The profile must derive the anchor key from the source URL and request only
    # that subtree — the tree is keyed by anchor id, so an unkeyed url lookup
    # would come back empty.
    scraper = FakeScraper({}, salesforce_tree_by_url={ANCHOR_ID: TREE})
    toc = await SalesforceProfile().build_toc(ROOT, scraper)
    assert [e.title for e in toc][0] == "Own from Salesforce"
    assert len(toc) == EXPECTED_ENTRY_COUNT


@pytest.mark.asyncio
async def test_build_toc_empty_expansion_returns_empty():
    toc = await SalesforceProfile().build_toc(ROOT, FakeScraper({}, salesforce_tree_by_url={ANCHOR_ID: []}))
    assert toc == []
