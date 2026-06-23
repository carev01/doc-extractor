"""Tests for the Salesforce Help extraction profile.

Salesforce Help renders its nav tree and article body inside shadow DOM
(Lightning Web Components), so the profile builds its TOC from the structured
data Browserless extracts via ``scraper.render`` ({toc:[{title,href,level}]}),
not from Firecrawl HTML. detect() still keys off the raw page markers.
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

ROOT = "https://help.salesforce.com/s/articleView?id=platform.own_from_salesforce.htm&type=5"


def _av(article_id: str) -> str:
    return f"articleView?id={article_id}&type=5"


# Canned Browserless render result mirroring the real shadow-DOM tree: a root
# (aria-level 1), the active article repeated at the top (dedup), and nested
# children at deeper aria-levels.
RENDER = {
    ROOT: {
        "toc": [
            {"title": "Own from Salesforce", "href": _av("platform.own_from_salesforce.htm"), "level": 1},
            # Active article repeats at the top of the tree — must dedup away.
            {"title": "Own from Salesforce", "href": _av("platform.own_from_salesforce.htm"), "level": 1},
            {"title": "Own from Salesforce Administration", "href": _av("platform.own_admin.htm"), "level": 2},
            {"title": "Manage API Tokens", "href": _av("platform.own_api_tokens.htm"), "level": 3},
            {"title": "Backups", "href": _av("platform.own_backups.htm"), "level": 2},
        ]
    }
}
EXPECTED_ENTRY_COUNT = 4  # 5 raw items, 1 duplicate


def _read(path: str) -> str:
    return open(path, encoding="utf-8").read()


def _scraper():
    return FakeScraper({}, render_by_url=RENDER)


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


# ── TOC building (from Browserless render data) ──────────────────────────────

@pytest.mark.asyncio
async def test_build_toc_dedup_and_count():
    toc = await SalesforceProfile().build_toc(ROOT, _scraper())
    assert len(toc) == EXPECTED_ENTRY_COUNT


@pytest.mark.asyncio
async def test_build_toc_levels_and_titles():
    toc = await SalesforceProfile().build_toc(ROOT, _scraper())
    got = [(e.title, e.level) for e in toc]
    assert got == [
        ("Own from Salesforce", 0),                 # aria-level 1 -> 0
        ("Own from Salesforce Administration", 1),  # aria-level 2 -> 1
        ("Manage API Tokens", 2),                   # aria-level 3 -> 2
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
async def test_build_toc_empty_render_returns_empty():
    toc = await SalesforceProfile().build_toc(ROOT, FakeScraper({}, render_by_url={ROOT: {}}))
    assert toc == []
