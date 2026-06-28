"""Tests for the rspress profile (full nav tree in static HTML, e.g. AvePoint Learn).

Rspress server-renders the complete sidebar (aside.rspress-sidebar) and the
article body (.rspress-doc) into every page's static HTML, then collapses the
sidebar to the current page after JS hydration -> we run on the raw_http path.

Hermetic: a FakeScraper serves canned HTML, no network.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.profiles.content_scope import scope_content_html
from app.services.profiles.rspress import RspressProfile

ROOT = "https://learn.avepoint.com/m365/about-cloud-backup.html"

# Representative Rspress page: rspress-sidebar (flat <h2>/<a>, no <ul>/<li>),
# a logo anchor to exclude, and an rspress-doc body with chrome to drop.
PAGE = """
<html><body>
  <nav class="rspress-nav"><a class="logo" href="/index.html">AvePoint Learn</a></nav>
  <aside class="sidebar_dd719 rspress-sidebar">
    <div class="logo-wrap"><a href="/index.html">AvePoint Learn</a></div>
    <div class="menu">
      <h2>About Cloud Backup</h2>
      <a href="/m365/about-cloud-backup/express.html"><div class="menuItem_ac22e">Express</div></a>
      <h2>Cloud Backup</h2>
      <a href="/m365/about-cloud-backup/cloud-backup/multigeo.html"><div class="menuItem_ac22e">Multi-Geo Support</div></a>
      <a href="/m365/whats-new.html"><div class="menuItem_ac22e">What's New</div></a>
      <h2>FAQs</h2>
      <a href="/m365/faqs/license.html"><div class="menuItem_ac22e">License and Subscription</div></a>
      <a href="/m365/faqs/storage.html"><div class="menuItem_ac22e">Storage</div></a>
    </div>
  </aside>
  <div class="rspress-doc">
    <h1>About Cloud Backup</h1>
    <nav class="in-doc-breadcrumb">Home / About</nav>
    <div><p>Cloud Backup ensures resiliency of service.</p></div>
    <div class="rspress-local-toc-container">On this page</div>
    <footer class="rspress-doc-footer">Previous Next Edit this page</footer>
  </div>
</body></html>
"""


def test_opts_into_raw_http():
    assert RspressProfile().content_engine == "raw_http"


def test_detect_needs_both_hooks():
    prof = RspressProfile()
    assert prof.detect(PAGE, ROOT) is True
    assert prof.detect('<div class="rspress-doc"></div>', ROOT) is False
    assert prof.detect('<aside class="rspress-sidebar"></aside>', ROOT) is False
    assert prof.detect("<html><body><p>hi</p></body></html>", "https://x/") is False


def test_content_scopes_doc_and_drops_chrome():
    cfg = RspressProfile().content_config()
    out = scope_content_html(PAGE, ROOT, cfg["includeTags"], cfg["excludeTags"])
    assert "resiliency of service" in out      # body kept
    assert "About Cloud Backup" in out         # h1 kept
    assert "On this page" not in out           # local TOC dropped
    assert "Edit this page" not in out         # footer dropped
    assert "Home / About" not in out           # in-doc nav dropped
    assert "License and Subscription" not in out  # sidebar outside scope


import pytest

from app.services.profiles.scraper import FakeScraper
from app.services.profiles.detector import detect_platform
from app.services.firecrawl import _resolve_toc_parents


def _scraper():
    return FakeScraper({}, raw_by_url={ROOT: PAGE})


def test_detects_via_registry():
    # Requires the registration added in this task.
    assert detect_platform(PAGE, ROOT) == "rspress"


@pytest.mark.asyncio
async def test_builds_nested_tree_in_order():
    toc = await RspressProfile().build_toc(ROOT, _scraper())
    shape = [(e.level, e.title, e.is_article) for e in toc]
    assert shape == [
        (0, "About Cloud Backup", False),          # h2 section, url=None
        (1, "Express", True),                      # /m365/about-cloud-backup/express.html
        (1, "Cloud Backup", False),                # nested h2 section
        (2, "Multi-Geo Support", True),            # depth-3 path -> level 2
        (0, "What's New", True),                   # /m365/whats-new.html
        (0, "FAQs", False),                        # h2 section
        (1, "License and Subscription", True),
        (1, "Storage", True),
    ]


@pytest.mark.asyncio
async def test_logo_and_cross_guide_anchors_excluded():
    toc = await RspressProfile().build_toc(ROOT, _scraper())
    assert all(e.url != "https://learn.avepoint.com/index.html" for e in toc)
    # Every article entry's path is under the guide root /m365/.
    arts = [e for e in toc if e.is_article]
    assert arts and all("/m365/" in e.url for e in arts)


@pytest.mark.asyncio
async def test_section_nodes_are_structural():
    toc = await RspressProfile().build_toc(ROOT, _scraper())
    faqs = next(e for e in toc if e.title == "FAQs")
    assert faqs.is_article is False
    assert faqs.url is None
    # Articles always carry a URL (so the pipeline scrapes them).
    assert all(e.url for e in toc if e.is_article)


@pytest.mark.asyncio
async def test_parents_resolve_section_as_parent():
    toc = await RspressProfile().build_toc(ROOT, _scraper())
    entries = [
        {"title": e.title, "url": e.url, "level": e.level,
         "is_article": e.is_article, "parent_url": e.parent_url}
        for e in toc
    ]
    parents = _resolve_toc_parents(entries)
    idx = {e["title"]: i for i, e in enumerate(entries)}
    # FAQs children nest under the FAQs section node.
    assert parents[idx["License and Subscription"]] == idx["FAQs"]
    assert parents[idx["Storage"]] == idx["FAQs"]
    # Multi-Geo nests under the nested "Cloud Backup" section.
    assert parents[idx["Multi-Geo Support"]] == idx["Cloud Backup"]
    # Top-level entries have no parent.
    assert parents[idx["What's New"]] is None


@pytest.mark.asyncio
async def test_missing_sidebar_returns_empty():
    s = FakeScraper({}, raw_by_url={ROOT: "<html><body><div class='rspress-doc'>x</div></body></html>"})
    assert await RspressProfile().build_toc(ROOT, s) == []
