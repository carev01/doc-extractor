import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.services.profiles.docusaurus import DocusaurusProfile
from app.services.profiles.scraper import FakeScraper

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "platforms", "docusaurus.html")
COMMVAULT_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "platforms", "commvault.html")
ROOT = "https://docs.portworx.com/portworx-backup-on-prem/"


def _html():
    return open(FIXTURE, encoding="utf-8").read()


def _commvault_html():
    return open(COMMVAULT_FIXTURE, encoding="utf-8").read()


def test_detect_matches_docusaurus():
    assert DocusaurusProfile().detect(_html(), ROOT) is True


def test_detect_rejects_commvault():
    assert DocusaurusProfile().detect(_commvault_html(), ROOT) is False


def test_content_config():
    cfg = DocusaurusProfile().content_config()
    assert cfg["includeTags"] == [".theme-doc-markdown"]
    assert cfg["onlyMainContent"] is False
    assert cfg["waitFor"] == 1500


# A fully-expanded Docusaurus sidebar (what Browserless returns after clicking
# every collapsed caret): a leaf link, a category with children, and a nested
# sub-category — the structure a single collapsed render can't expose.
EXPANDED_SIDEBAR = """
<ul class="theme-doc-sidebar-menu menu__list">
  <li class="theme-doc-sidebar-item-link">
    <a class="menu__link" href="/portworx-backup-on-prem/whats-new">What's New</a>
  </li>
  <li class="theme-doc-sidebar-item-category menu__list-item">
    <div class="menu__list-item-collapsible">
      <a class="menu__link menu__link--sublist" href="/portworx-backup-on-prem/concepts">Concepts</a>
      <button aria-expanded="true" class="clean-btn menu__caret" type="button"></button>
    </div>
    <ul class="menu__list">
      <li class="theme-doc-sidebar-item-link">
        <a class="menu__link" href="/portworx-backup-on-prem/concepts/health-check">Health Check</a>
      </li>
      <li class="theme-doc-sidebar-item-category menu__list-item">
        <div class="menu__list-item-collapsible">
          <a class="menu__link menu__link--sublist" href="/portworx-backup-on-prem/concepts/api">API</a>
          <button aria-expanded="true" class="clean-btn menu__caret" type="button"></button>
        </div>
        <ul class="menu__list">
          <li class="theme-doc-sidebar-item-link">
            <a class="menu__link" href="/portworx-backup-on-prem/concepts/api/backend">Backend API</a>
          </li>
        </ul>
      </li>
    </ul>
  </li>
</ul>
"""


@pytest.mark.asyncio
async def test_build_toc_expands_full_nested_tree():
    """When Browserless returns the expanded sidebar, the full nested hierarchy
    is parsed (not just the collapsed top level)."""
    scraper = FakeScraper({ROOT: _html()}, docusaurus_sidebar_by_url={ROOT: EXPANDED_SIDEBAR})
    toc = await DocusaurusProfile().build_toc(ROOT, scraper)

    by_title = {e.title: e for e in toc}
    assert set(by_title) == {"What's New", "Concepts", "Health Check", "API", "Backend API"}
    # Levels reflect nesting depth.
    assert by_title["What's New"].level == 0
    assert by_title["Concepts"].level == 0
    assert by_title["Health Check"].level == 1
    assert by_title["API"].level == 1
    assert by_title["Backend API"].level == 2
    # Categories with children are sections; leaves are articles.
    assert by_title["Concepts"].is_article is False
    assert by_title["API"].is_article is False
    assert by_title["Backend API"].is_article is True
    # Relative hrefs are resolved against the root.
    assert by_title["Backend API"].url == ROOT + "concepts/api/backend"


@pytest.mark.asyncio
async def test_build_toc_falls_back_to_single_render_without_browserless():
    """If Browserless can't expand (no fixture → BrowserlessError), the profile
    still returns a TOC from a single render (top level only)."""
    toc = await DocusaurusProfile().build_toc(ROOT, FakeScraper({ROOT: _html()}))
    assert all(e.title and e.url for e in toc)
    titles = [e.title for e in toc]
    assert "Portworx Backup Documentation" in titles
    assert "Release Notes" in titles
    # The collapsed fixture exposes exactly its 11 top-level items.
    assert len(toc) == 11
    assert titles.index("Portworx Backup Documentation") < titles.index("Release Notes")
