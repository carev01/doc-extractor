"""Tests for the grouped_nav profile (heading-grouped static sidebar; e.g. Velero).

The sidebar is a single ``nav.navigation`` of ``<h3>`` group headings each
followed by a flat ``<ul>`` of links; a version ``.dropdown`` sits in the same
column but outside the nav. Content lives in ``.documentation-container`` with a
right-rail ``nav#TableOfContents`` to drop.

Hermetic: a FakeScraper serves a canned landing page, no network.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from app.services.profiles.scraper import FakeScraper
from app.services.profiles.detector import detect_platform
from app.services.profiles.content_scope import scope_content_html
from app.services.profiles.grouped_nav import GroupedNavProfile

ROOT = "https://velero.io/docs/v1.18/"

LANDING = """
<html><body>
  <div class="col-md-3 toc">
    <div class="dropdown mb-2">
      <button class="btn dropdown-toggle">v1.18</button>
      <div class="dropdown-menu">
        <a class="dropdown-item" href="/docs/main/">main</a>
        <a class="dropdown-item" href="/docs/v1.17/">v1.17</a>
      </div>
    </div>
    <nav class="navigation">
      <h3>Introduction</h3>
      <ul>
        <li><a href="/docs/v1.18/">About Velero</a></li>
        <li><a href="/docs/v1.18/how-velero-works">How Velero works</a></li>
      </ul>
      <h3>Use</h3>
      <ul>
        <li><a href="/docs/v1.18/file-system-backup">File system backup</a></li>
        <li><a href="https://github.com/vmware-tanzu/velero">GitHub</a></li>
      </ul>
      <h3>Troubleshoot</h3>
      <ul>
        <li><a href="/docs/v1.18/file-system-backup#troubleshooting">Troubleshoot FSB</a></li>
        <li><a href="/docs/v1.17/old-page">Old version page</a></li>
      </ul>
    </nav>
  </div>
  <div class="col-md-8"><div class="documentation-container"><h1>Overview</h1></div></div>
</body></html>
"""


def _scraper():
    return FakeScraper({}, raw_by_url={ROOT: LANDING})


def test_opts_into_raw_http():
    assert GroupedNavProfile().content_engine == "raw_http"


def test_detects_via_registry():
    assert detect_platform(LANDING, ROOT) == "grouped_nav"


def test_detect_needs_both_markers():
    prof = GroupedNavProfile()
    assert prof.detect(LANDING, ROOT) is True
    # nav alone or container alone is too generic — require both.
    assert prof.detect('<nav class="navigation"></nav>', ROOT) is False
    assert prof.detect('<div class="documentation-container"></div>', ROOT) is False
    assert prof.detect("<html><body><p>hi</p></body></html>", "https://x/") is False


@pytest.mark.asyncio
async def test_builds_grouped_tree_in_order():
    toc = await GroupedNavProfile().build_toc(ROOT, _scraper())
    shape = [(e.level, e.title, e.is_article) for e in toc]
    assert shape == [
        (0, "Introduction", False),
        (1, "About Velero", True),
        (1, "How Velero works", True),
        (0, "Use", False),
        (1, "File system backup", True),
        (0, "Troubleshoot", False),
        # the #troubleshooting anchor and the v1.17 link are both dropped
    ]


@pytest.mark.asyncio
async def test_version_dropdown_and_external_links_excluded():
    toc = await GroupedNavProfile().build_toc(ROOT, _scraper())
    urls = [e.url for e in toc if e.url]
    assert all(u.startswith(ROOT) for u in urls)
    assert not any("/docs/main/" in u or "/docs/v1.17/" in u for u in urls)
    assert not any("github.com" in u for u in urls)


@pytest.mark.asyncio
async def test_anchor_duplicate_deduped_and_trailing_slash():
    toc = await GroupedNavProfile().build_toc(ROOT, _scraper())
    fsb = [e for e in toc if e.url and e.url.endswith("file-system-backup/")]
    assert len(fsb) == 1                       # #troubleshooting variant deduped
    assert fsb[0].url == ROOT + "file-system-backup/"  # canonical trailing slash


@pytest.mark.asyncio
async def test_missing_nav_returns_empty():
    s = FakeScraper({}, raw_by_url={ROOT: "<html><body><p>no nav</p></body></html>"})
    assert await GroupedNavProfile().build_toc(ROOT, s) == []


def test_content_scopes_container_and_drops_mini_toc():
    cfg = GroupedNavProfile().content_config()
    assert cfg["includeTags"] == [".documentation-container"]
    html = (
        '<html><body><nav class="navigation"><h3>Use</h3></nav>'
        '<div class="documentation-container">'
        '  <h1>How Velero Works</h1>'
        '  <aside><nav id="TableOfContents"><ul><li>On-demand backups</li></ul></nav></aside>'
        '  <p>Velero treats object storage as the source of truth.</p>'
        '</div></body></html>'
    )
    out = scope_content_html(
        html, "https://velero.io/docs/v1.18/how-velero-works/",
        cfg["includeTags"], cfg["excludeTags"],
    )
    assert "source of truth" in out          # prose kept
    assert "How Velero Works" in out         # title kept
    assert 'id="TableOfContents"' not in out  # mini-TOC dropped
    assert "On-demand backups" not in out
    assert "Use" not in out                  # sidebar nav outside scope
