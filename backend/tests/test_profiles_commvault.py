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
