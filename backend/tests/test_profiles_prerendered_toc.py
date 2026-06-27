"""Tests for the prerendered_toc profile (full nav tree in static HTML; e.g. Veeam).

The complete nested TOC is server-rendered into every page under
``.page-toc .page-toc__search-links`` (a list the search box filters); the topic
body is ``article.js-page-article``. Both static, so raw_http.

Hermetic: a FakeScraper serves canned HTML, no network.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from app.services.profiles.scraper import FakeScraper
from app.services.profiles.detector import detect_platform
from app.services.profiles.content_scope import scope_content_html
from app.services.profiles.prerendered_toc import PrerenderedTocProfile

ROOT = "https://helpcenter.veeam.com/docs/vbr/userguide/overview.html"

PAGE = """
<html><body>
  <nav class="page-toc mobile-hidden js-page-toc">
    <div class="page-toc__search js-toc-search"><input></div>
    <ul class="page-toc__search-links js-toc-search-links">
      <li class="current"><a href="overview.html">About Veeam Backup &amp; Replication</a></li>
      <li class="has-list">
        <a href="planning.html">Planning and Preparation</a><i></i>
        <ul>
          <li class="has-list">
            <a href="platform_support.html">Workloads</a><i></i>
            <ul>
              <li><a href="platform_support_vm.html">VMware vSphere</a></li>
              <li><a href="platform_support_hv.html">Microsoft Hyper-V</a></li>
            </ul>
          </li>
        </ul>
      </li>
    </ul>
  </nav>
  <div class="content">
    <div class="topic"><div class="topic__inner">
      <article class="js-page-article">
        <h1>About Veeam Backup &amp; Replication</h1>
        <aside class="mini-toc__container">In this article</aside>
        <div><p>Veeam Backup &amp; Replication is a data protection solution.</p></div>
        <footer>Page updated 6/5/2026 Send feedback</footer>
      </article>
    </div></div>
  </div>
</body></html>
"""


def _scraper():
    return FakeScraper({}, raw_by_url={ROOT: PAGE})


def test_opts_into_raw_http():
    assert PrerenderedTocProfile().content_engine == "raw_http"


def test_detects_via_registry():
    assert detect_platform(PAGE, ROOT) == "prerendered_toc"


def test_detect_needs_both_hooks():
    prof = PrerenderedTocProfile()
    assert prof.detect(PAGE, ROOT) is True
    assert prof.detect('<nav class="js-page-toc"></nav>', ROOT) is False
    assert prof.detect('<article class="js-page-article"></article>', ROOT) is False
    assert prof.detect("<html><body><p>hi</p></body></html>", "https://x/") is False


@pytest.mark.asyncio
async def test_builds_nested_tree_in_order():
    toc = await PrerenderedTocProfile().build_toc(ROOT, _scraper())
    shape = [(e.level, e.title, e.is_article) for e in toc]
    assert shape == [
        (0, "About Veeam Backup & Replication", True),   # leaf
        (0, "Planning and Preparation", False),          # parent: section, but…
        (1, "Workloads", False),
        (2, "VMware vSphere", True),
        (2, "Microsoft Hyper-V", True),
    ]


@pytest.mark.asyncio
async def test_parent_topics_keep_url_and_are_scraped():
    toc = await PrerenderedTocProfile().build_toc(ROOT, _scraper())
    # Parent topics are real landing pages: not is_article, but carry a URL so
    # the pipeline (scrapable = any entry with a url) still fetches them.
    base = "https://helpcenter.veeam.com/docs/vbr/userguide/"
    planning = next(e for e in toc if e.title == "Planning and Preparation")
    assert planning.is_article is False
    assert planning.url == base + "planning.html"          # relative href resolved
    assert all(e.url for e in toc)                          # every node has a URL


@pytest.mark.asyncio
async def test_missing_nav_returns_empty():
    s = FakeScraper({}, raw_by_url={ROOT: "<html><body><p>no nav</p></body></html>"})
    assert await PrerenderedTocProfile().build_toc(ROOT, s) == []


def test_content_scopes_article_and_drops_chrome():
    cfg = PrerenderedTocProfile().content_config()
    out = scope_content_html(PAGE, ROOT, cfg["includeTags"], cfg["excludeTags"])
    assert "data protection solution" in out          # body kept
    assert "About Veeam Backup" in out                # h1 kept
    assert "In this article" not in out               # mini-TOC dropped
    assert "Send feedback" not in out                 # footer dropped
    assert "Planning and Preparation" not in out      # sidebar outside scope
