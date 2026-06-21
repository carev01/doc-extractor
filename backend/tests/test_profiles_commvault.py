import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.services.profiles.commvault import CommvaultProfile
from app.services.profiles.scraper import FakeScraper

ROOT = "https://documentation.commvault.com/11.44/software/get_started_with_commvault.html"

# Canned Browserless-rendered nav: the active "Get started" section (nav-open)
# expanded with its children, plus a collapsed sibling section.
RENDERED_NAV = """
<html><body>
<div id="nav" class="nav">
  <ul>
    <li class="nav-row nav-open">
      <div class="nav-item"><a href="get_started_with_commvault.html">Get started</a></div>
      <ul>
        <li class="nav-row"><div class="nav-item"><a href="deploy_commvault_infrastructure.html">Deploy infrastructure</a></div></li>
        <li class="nav-row"><div class="nav-item"><a href="configure_network_connectivity_for_commvault.html">Configure network connectivity</a></div></li>
      </ul>
    </li>
    <li class="nav-row"><div class="nav-item"><a href="what_s_new.html">What's new</a></div></li>
    <li class="nav-row"><div class="nav-item"><a href="explore.html">Explore</a></div></li>
  </ul>
</div>
</body></html>
"""


def test_detect_matches_commvault_host():
    assert CommvaultProfile().detect("<html>Loading…</html>", ROOT) is True


def test_detect_matches_old_inline_nav():
    html = '<div id="nav"><ul class="nav-group"></ul></div>'
    assert CommvaultProfile().detect(html, "https://docs.example.com/x.html") is True


def test_detect_rejects_other_platforms():
    assert CommvaultProfile().detect(
        "<html><body><main>hi</main></body></html>", "https://example.com/"
    ) is False


def test_content_config_scopes_to_doc():
    cfg = CommvaultProfile().content_config()
    assert cfg["includeTags"] == ["#doc"]
    assert cfg["excludeTags"] == [".breadcrumbs"]  # drop the leading breadcrumb trail


@pytest.mark.asyncio
async def test_build_toc_scoped_hierarchy():
    """TOC is scoped to the active section and nests via the rendered <ul> tree."""
    scraper = FakeScraper({}, rendered_html_by_url={ROOT: RENDERED_NAV})
    toc = await CommvaultProfile().build_toc(ROOT, scraper)
    got = [(e.title, e.level, e.is_article) for e in toc]
    assert got == [
        ("Get started", 0, False),                  # section root (has children)
        ("Deploy infrastructure", 1, True),
        ("Configure network connectivity", 1, True),
    ]
    # 'What's new' / 'Explore' (other top-level sections) are NOT included.
    assert "What's new" not in [e.title for e in toc]


@pytest.mark.asyncio
async def test_build_toc_parent_and_absolute_urls():
    scraper = FakeScraper({}, rendered_html_by_url={ROOT: RENDERED_NAV})
    toc = await CommvaultProfile().build_toc(ROOT, scraper)
    by_title = {e.title: e for e in toc}
    assert by_title["Get started"].parent_url is None
    assert by_title["Deploy infrastructure"].parent_url == by_title["Get started"].url
    for e in toc:
        assert e.url.startswith("https://documentation.commvault.com/11.44/software/")


@pytest.mark.asyncio
async def test_build_toc_empty_when_nav_not_rendered():
    scraper = FakeScraper({}, rendered_html_by_url={ROOT: "<html><body>Loading…</body></html>"})
    assert await CommvaultProfile().build_toc(ROOT, scraper) == []


# ── FULL mode (rooted at index.html) ────────────────────────────────────────

FULL_BASE = "https://documentation.commvault.com/11.44/software/"
INDEX_ROOT = FULL_BASE + "index.html"


def _page(navpath_keys, title):
    meta = "[" + ", ".join(f"&#34;{k}&#34;" for k in navpath_keys) + "]"
    return f'<html><head><meta name="nav-path" content="{meta}"></head>' \
           f'<body><div id="doc"><h1 class="heading">{title}</h1></div></body></html>'


FULL_RAW = {
    FULL_BASE + "static/scripts/nav-map.json": json.dumps([
        "index.html", "get_started_with_commvault.html",
        "deploy_infra.html", "what_s_new.html",
    ]),
    FULL_BASE + "index.html": _page(["index"], "Software"),
    FULL_BASE + "get_started_with_commvault.html":
        _page(["index", "get_started_with_commvault"], "Get started"),
    FULL_BASE + "deploy_infra.html":
        _page(["index", "get_started_with_commvault", "deploy_infra"], "Deploy infrastructure"),
    # HTML-entity title — must be decoded to "What's new".
    FULL_BASE + "what_s_new.html": _page(["index", "what_s_new"], "What&#39;s new"),
}


@pytest.mark.asyncio
async def test_full_mode_builds_whole_hierarchical_tree():
    """Rooted at index.html → full doc set, hierarchy from each page's nav-path."""
    scraper = FakeScraper({}, raw_by_url=FULL_RAW)
    toc = await CommvaultProfile().build_toc(INDEX_ROOT, scraper)
    got = [(e.title, e.level, e.url) for e in toc]
    assert got == [
        ("Software", 0, FULL_BASE + "index.html"),
        ("Get started", 1, FULL_BASE + "get_started_with_commvault.html"),
        ("Deploy infrastructure", 2, FULL_BASE + "deploy_infra.html"),
        ("What's new", 1, FULL_BASE + "what_s_new.html"),
    ]


@pytest.mark.asyncio
async def test_full_mode_parent_linkage():
    scraper = FakeScraper({}, raw_by_url=FULL_RAW)
    toc = await CommvaultProfile().build_toc(INDEX_ROOT, scraper)
    by = {e.title: e for e in toc}
    assert by["Software"].parent_url is None
    assert by["Get started"].parent_url == by["Software"].url
    assert by["Deploy infrastructure"].parent_url == by["Get started"].url
    assert by["What's new"].parent_url == by["Software"].url


@pytest.mark.asyncio
async def test_full_mode_empty_when_navmap_missing():
    scraper = FakeScraper({}, raw_by_url={})  # nav-map.json fetch fails
    assert await CommvaultProfile().build_toc(INDEX_ROOT, scraper) == []
