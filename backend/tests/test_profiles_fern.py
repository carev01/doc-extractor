"""Tests for the fern profile (Fern docs, e.g. docs.eon.io).

Fern server-renders the full sidebar (aside.fern-sidebar-desktop, a nested
<ul>/<li> tree) and the article body (.fern-prose) into static HTML, so the
profile runs on the raw_http path (with the realm session injected for
authenticated sources). Hermetic: FakeScraper serves canned HTML.
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

PAGE = """
<html><body>
  <aside class="fern-sidebar-desktop">
    <ul>
      <li><a href="/user-guide/what-is-eon">What Is Eon</a></li>
      <li>
        <a href="/user-guide/access-management/about-access-management">Access Management</a>
        <ul>
          <li><a href="/user-guide/access-management/api-credentials/about-api-credentials">API Credentials</a></li>
        </ul>
      </li>
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
async def test_builds_nested_tree():
    toc = await FernProfile().build_toc(ROOT, _scraper())
    shape = [(e.level, e.title, e.is_article) for e in toc]
    assert shape == [
        (0, "What Is Eon", True),
        (0, "Access Management", False),
        (1, "API Credentials", True),
    ]
    assert all(e.url for e in toc)


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
    assert "Access Management" not in out       # sidebar outside scope
