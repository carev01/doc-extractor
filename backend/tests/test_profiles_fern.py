"""Tests for the fern profile (Fern docs, e.g. docs.eon.io).

Fern server-renders the article body (.fern-prose) and all sidebar links into
static HTML, so the profile runs on the raw_http path (with the realm session
injected for authenticated sources). In the raw (no-JS) sidebar the page links
sit in a flat list alongside a tab switcher and collapsible-section <button>s,
so build_toc collects every in-guide anchor and derives nesting from URL path
depth. Hermetic: FakeScraper serves canned HTML.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from app.services.profiles.scraper import FakeScraper
from app.services.profiles.detector import detect_platform
from app.services.profiles.content_scope import scope_content_html
from app.services.profiles.fern import FernProfile

ROOT = "https://docs.eon.io/user-guide/what-is-eon"

# Mirrors Fern's raw sidebar: a tab switcher + a flat page-link list (not a
# nested <ul>/<li> tree), plus a different-tab (/api) link and a duplicate.
PAGE = """
<html><body>
  <aside class="fern-sidebar-desktop">
    <ul><li><a href="/user-guide/what-is-eon">User Guide</a></li>
        <li><a href="/api/overview">API Reference</a></li></ul>
    <button>AWS</button>
    <ul>
      <li><a href="/user-guide/explore-the-console">Explore the Console</a></li>
      <li><a href="/user-guide/aws/resources/ec2">EC2</a></li>
      <li><a href="/user-guide/faqs/billing">Billing</a></li>
      <li><a href="/user-guide/what-is-eon">What Is Eon (dup)</a></li>
      <li><a href="/api/auth">API Auth</a></li>
    </ul>
  </aside>
  <main class="fern-main">
    <article class="w-content-width">
      <div class="fern-prose">
        <h1>What Is Eon?</h1>
        <p>Eon is a cloud backup platform.</p>
      </div>
      <div class="toc-root">On this page</div>
    </article>
    <footer>Was this page helpful?</footer>
  </main>
</body></html>
"""


def _scraper():
    return FakeScraper({}, raw_by_url={ROOT: PAGE})


def test_opts_into_raw_http():
    assert FernProfile().content_engine == "raw_http"


def test_detect_needs_both_hooks():
    prof = FernProfile()
    assert prof.detect(PAGE, ROOT) is True
    assert prof.detect('<aside class="fern-sidebar-desktop"></aside>', ROOT) is False
    assert prof.detect('<div class="fern-prose"></div>', ROOT) is False
    assert prof.detect("<html><body><p>hi</p></body></html>", "https://x/") is False


def test_detects_via_registry():
    assert detect_platform(PAGE, ROOT) == "fern"


@pytest.mark.asyncio
async def test_builds_toc_levels_from_url_depth():
    toc = await FernProfile().build_toc(ROOT, _scraper())
    shape = [(e.level, e.title, e.is_article) for e in toc]
    # Only in-guide (/user-guide) links, de-duped, in DOM order; level = path depth.
    assert shape == [
        (0, "User Guide", True),                 # /user-guide/what-is-eon
        (0, "Explore the Console", True),        # /user-guide/explore-the-console
        (2, "EC2", True),                        # /user-guide/aws/resources/ec2
        (1, "Billing", True),                    # /user-guide/faqs/billing
    ]
    assert all(e.url and e.is_article for e in toc)


@pytest.mark.asyncio
async def test_excludes_other_tab_and_dedupes():
    toc = await FernProfile().build_toc(ROOT, _scraper())
    urls = [e.url for e in toc]
    assert not any("/api/" in u for u in urls)            # other tab excluded
    assert urls.count("https://docs.eon.io/user-guide/what-is-eon") == 1  # deduped


@pytest.mark.asyncio
async def test_missing_sidebar_returns_empty():
    s = FakeScraper({}, raw_by_url={ROOT: "<html><body><div class='fern-prose'>x</div></body></html>"})
    assert await FernProfile().build_toc(ROOT, s) == []


def test_content_scopes_prose_and_drops_chrome():
    cfg = FernProfile().content_config()
    out = scope_content_html(PAGE, ROOT, cfg["includeTags"], cfg["excludeTags"])
    assert "cloud backup platform" in out      # body kept
    assert "What Is Eon?" in out               # h1 kept
    assert "On this page" not in out           # right-rail TOC dropped
    assert "Was this page helpful" not in out  # footer dropped
    assert "Explore the Console" not in out     # sidebar outside scope
