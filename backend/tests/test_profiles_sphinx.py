"""Tests for the Sphinx (ReadTheDocs theme) profile (Bacula).

The full tree isn't on any one page: the home toctree is hidden (sidebar only),
and each page renders just its direct children as a .toctree-wrapper. The profile
seeds from the home sidebar's toctree-l1 sections, crawls each page's direct
children breadth-first, and assembles depth-first to preserve curated order.

Hermetic: a FakeScraper serves canned HTML per URL, no network.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from app.services.profiles.scraper import FakeScraper
from app.services.profiles.detector import detect_platform
from app.services.profiles.content_scope import scope_content_html
from app.services.profiles.sphinx import SphinxProfile

BASE = "https://docs.example.com"
ROOT = f"{BASE}/index.html"


def test_opts_into_raw_http():
    assert SphinxProfile().content_engine == "raw_http"


def test_detects_on_rtd_theme_markers():
    html = '<nav class="wy-nav-side"><div class="wy-menu-vertical"></div></nav>'
    assert detect_platform(html, ROOT) == "sphinx"


def test_detect_negative_on_plain_html():
    assert SphinxProfile().detect("<html><body><p>hi</p></body></html>", ROOT) is False


def _toctree(children):
    lis = "".join(
        f'<li class="toctree-l1"><a class="reference internal" href="{h}">{t}</a></li>'
        for h, t in children
    )
    return f'<div class="toctree-wrapper"><ul>{lis}</ul></div>'


def _page(body_inner):
    return f'<html><body><div role="main">{body_inner}</div></body></html>'


def _scraper():
    # Home: hidden master toctree -> NO .toctree-wrapper in body; sidebar carries
    # the two top-level sections.
    home = (
        '<html><body>'
        '<nav class="wy-menu-vertical">'
        '  <li class="toctree-l1"><a class="reference internal" href="install/index.html">Installation</a></li>'
        '  <li class="toctree-l1"><a class="reference internal" href="config/index.html">Configuration</a></li>'
        '</nav>'
        '<div role="main"><p>Home intro, no toctree here.</p></div>'
        '</body></html>'
    )
    install = _page(
        '<h1>Installation</h1>'
        + _toctree([("EnterpriseInstall/index.html", "Enterprise"),
                    ("CommunityInstall/index.html", "Community")])
    )
    enterprise = _page('<h1>Enterprise</h1>' + _toctree([("linux.html", "On Linux")]))
    linux = _page('<h1>On Linux</h1><p>Install the package.</p>')
    community = _page('<h1>Community</h1><p>Community steps.</p>')
    config = _page('<h1>Configuration</h1><p>Configure it.</p>')
    return FakeScraper({}, raw_by_url={
        ROOT: home,
        f"{BASE}/install/index.html": install,
        f"{BASE}/install/EnterpriseInstall/index.html": enterprise,
        f"{BASE}/install/EnterpriseInstall/linux.html": linux,
        f"{BASE}/install/CommunityInstall/index.html": community,
        f"{BASE}/config/index.html": config,
    })


@pytest.mark.asyncio
async def test_builds_full_tree_depth_first_in_curated_order():
    toc = await SphinxProfile().build_toc(ROOT, _scraper())
    shape = [(e.level, e.title) for e in toc]
    assert shape == [
        (0, "Installation"),
        (1, "Enterprise"),
        (2, "On Linux"),       # reached by recursing into Enterprise's page
        (1, "Community"),
        (0, "Configuration"),
    ]


@pytest.mark.asyncio
async def test_all_entries_have_resolved_urls():
    toc = await SphinxProfile().build_toc(ROOT, _scraper())
    by_title = {e.title: e.url for e in toc}
    assert by_title["On Linux"] == f"{BASE}/install/EnterpriseInstall/linux.html"
    assert all(e.url for e in toc)  # every node is a real page


@pytest.mark.asyncio
async def test_empty_when_no_sidebar_sections():
    scr = FakeScraper({}, raw_by_url={ROOT: "<html><body><div role='main'></div></body></html>"})
    assert await SphinxProfile().build_toc(ROOT, scr) == []


@pytest.mark.asyncio
async def test_crawl_tolerates_a_missing_page():
    # A child page that fails to fetch: its own children are skipped, the rest of
    # the tree is intact (no exception).
    scr = _scraper()
    del scr._raw[f"{BASE}/install/EnterpriseInstall/index.html"]
    toc = await SphinxProfile().build_toc(ROOT, scr)
    titles = [e.title for e in toc]
    assert "Enterprise" in titles and "On Linux" not in titles  # header kept, child skipped
    assert "Community" in titles and "Configuration" in titles  # unaffected


def test_content_scopes_role_main_and_drops_toctree_and_chrome():
    cfg = SphinxProfile().content_config()
    assert cfg["includeTags"] == ["[role=main]"]
    html = (
        '<html><body><nav class="wy-nav-side">SIDEBAR</nav>'
        '<div role="main">'
        '  <div class="wy-breadcrumbs">Docs &raquo; Install</div>'
        '  <h1>On Linux<a class="headerlink" href="#on-linux">¶</a></h1>'
        '  <p>Real install prose.</p>'
        '  <div class="toctree-wrapper"><ul><li><a href="x.html">child nav</a></li></ul></div>'
        '  <footer class="rst-footer-buttons"><a>Next</a></footer>'
        '</div></body></html>'
    )
    out = scope_content_html(
        html, f"{BASE}/install/EnterpriseInstall/linux.html",
        cfg["includeTags"], cfg["excludeTags"],
    )
    assert "Real install prose." in out
    assert "On Linux" in out             # title kept
    assert "SIDEBAR" not in out          # outside [role=main]
    assert "child nav" not in out        # in-body toctree dropped
    assert "Docs" not in out             # breadcrumb dropped
    assert "Next" not in out             # footer nav dropped
    assert "¶" not in out                # headerlink dropped
