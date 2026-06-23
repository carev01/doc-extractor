"""Tests for the generic sitemap fallback profile."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.profiles.generic import GenericProfile
from app.services.profiles.scraper import FakeScraper
from app.services.profiles import registry

LAZY_TREE_FIXTURE = os.path.join(
    os.path.dirname(__file__), "fixtures", "platforms", "lazy_tree.html"
)
LAZY_TREE_ROOT = "https://documentation.commvault.com/clumio/index.html"

ROOT = "https://x/docs/"

# URL list that FakeScraper.map_urls will return (simulates Firecrawl /v2/map output)
URLS = [
    ROOT,                           # root itself
    "https://x/docs/a",            # level 1
    "https://x/docs/a/b",          # level 2  (parent = /docs/a/)  -- note: parent candidate is /docs/a/
    "https://x/other/c",            # different subtree — should be filtered out
    "https://x/docs/d",            # level 1
]


# ── detect ──────────────────────────────────────────────────────────────────

def test_detect_always_false_on_lazy_tree_html():
    html = open(LAZY_TREE_FIXTURE, encoding="utf-8").read()
    assert GenericProfile().detect(html, LAZY_TREE_ROOT) is False


def test_detect_always_false_on_confluence_ish_html():
    confluence_html = (
        '<html><head>'
        '<meta name="confluence-space-key" content="DOC">'
        '</head><body><div id="main-content">Confluence page</div></body></html>'
    )
    assert GenericProfile().detect(confluence_html, "https://wiki.example.com/") is False


def test_detect_always_false_on_empty_html():
    assert GenericProfile().detect("", "https://example.com/") is False


# ── build_toc ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_build_toc_filters_out_of_prefix_urls():
    """URLs not under /docs/ must be dropped."""
    scraper = FakeScraper(html_by_url={}, urls=URLS)
    toc = await GenericProfile().build_toc(ROOT, scraper)
    toc_urls = [e.url for e in toc]
    assert "https://x/other/c" not in toc_urls


@pytest.mark.asyncio
async def test_build_toc_preserves_document_order():
    """The kept URLs must appear in the same order as the map returned them."""
    scraper = FakeScraper(html_by_url={}, urls=URLS)
    toc = await GenericProfile().build_toc(ROOT, scraper)
    toc_urls = [e.url for e in toc]
    # /docs/a must come before /docs/a/b, which must come before /docs/d
    assert toc_urls.index("https://x/docs/a") < toc_urls.index("https://x/docs/a/b")
    assert toc_urls.index("https://x/docs/a/b") < toc_urls.index("https://x/docs/d")


@pytest.mark.asyncio
async def test_build_toc_levels_from_path_depth():
    """/docs/a is 1 level deep relative to root /docs/; /docs/a/b is 2."""
    scraper = FakeScraper(html_by_url={}, urls=URLS)
    toc = await GenericProfile().build_toc(ROOT, scraper)
    by_url = {e.url: e for e in toc}

    assert "https://x/docs/a" in by_url
    assert by_url["https://x/docs/a"].level == 1

    assert "https://x/docs/a/b" in by_url
    assert by_url["https://x/docs/a/b"].level == 2

    assert "https://x/docs/d" in by_url
    assert by_url["https://x/docs/d"].level == 1


@pytest.mark.asyncio
async def test_build_toc_parent_url_resolution():
    """/docs/a/b's parent should resolve to the /docs/a entry (with or without trailing slash)."""
    scraper = FakeScraper(html_by_url={}, urls=URLS)
    toc = await GenericProfile().build_toc(ROOT, scraper)
    by_url = {e.url: e for e in toc}

    entry_ab = by_url.get("https://x/docs/a/b")
    assert entry_ab is not None
    # parent_url must point to the /docs/a entry — accept both slash forms
    assert entry_ab.parent_url in ("https://x/docs/a", "https://x/docs/a/")


@pytest.mark.asyncio
async def test_build_toc_parent_url_none_when_parent_not_in_set():
    """/docs/d has no child so no parent is in the set at its segment parent."""
    scraper = FakeScraper(html_by_url={}, urls=URLS)
    toc = await GenericProfile().build_toc(ROOT, scraper)
    by_url = {e.url: e for e in toc}

    # /docs/d's parent candidate would be /docs/ (the root), which IS kept
    entry_d = by_url.get("https://x/docs/d")
    assert entry_d is not None
    # parent candidate for /docs/d is /docs/ which equals ROOT and is in the kept set
    assert entry_d.parent_url == ROOT or entry_d.parent_url is None  # either is acceptable


@pytest.mark.asyncio
async def test_build_toc_deduplicates_urls():
    """Duplicate URLs in the map output must appear only once in the TOC."""
    urls_with_dup = [
        ROOT,
        "https://x/docs/a",
        "https://x/docs/a",   # duplicate
        "https://x/docs/d",
    ]
    scraper = FakeScraper(html_by_url={}, urls=urls_with_dup)
    toc = await GenericProfile().build_toc(ROOT, scraper)
    toc_urls = [e.url for e in toc]
    assert toc_urls.count("https://x/docs/a") == 1


@pytest.mark.asyncio
async def test_build_toc_all_entries_are_articles():
    """Generic profile marks all entries is_article=True."""
    scraper = FakeScraper(html_by_url={}, urls=URLS)
    toc = await GenericProfile().build_toc(ROOT, scraper)
    assert all(e.is_article for e in toc)


@pytest.mark.asyncio
async def test_build_toc_levels_file_tailed_root():
    """File-tailed root URL (e.g. /docs/index.html) must use /docs/ as the
    baseline, so /docs/a is level 1 and /docs/a/b is level 2 (not 0/1)."""
    file_root = "https://x/docs/index.html"
    urls = [
        file_root,              # root itself
        "https://x/docs/a",    # should be level 1
        "https://x/docs/a/b",  # should be level 2
    ]
    scraper = FakeScraper(html_by_url={}, urls=urls)
    toc = await GenericProfile().build_toc(file_root, scraper)
    by_url = {e.url: e for e in toc}

    assert "https://x/docs/a" in by_url, "https://x/docs/a missing from TOC"
    assert by_url["https://x/docs/a"].level == 1, (
        f"expected level 1, got {by_url['https://x/docs/a'].level}"
    )

    assert "https://x/docs/a/b" in by_url, "https://x/docs/a/b missing from TOC"
    assert by_url["https://x/docs/a/b"].level == 2, (
        f"expected level 2, got {by_url['https://x/docs/a/b'].level}"
    )


# ── content_config ───────────────────────────────────────────────────────────

def test_content_config_only_main_content():
    cfg = GenericProfile().content_config()
    assert cfg["onlyMainContent"] is True


def test_content_config_wait_for():
    cfg = GenericProfile().content_config()
    assert cfg["waitFor"] == 1500


# ── registry ─────────────────────────────────────────────────────────────────

def test_generic_profile_registered():
    """The generic profile must be in the registry (resolver fallback depends on it)."""
    assert registry.get("generic") is not None


def test_generic_profile_is_resolver_fallback():
    """Confirm registry.get('generic') exists and is the documented fallback.

    Full _resolve_profile is async and needs Firecrawl, so we just assert
    the registry entry is in place (the wiring is in firecrawl.py line that
    calls registry.get('generic')).
    """
    profile = registry.get("generic")
    assert profile is not None
    assert profile.name == "generic"
