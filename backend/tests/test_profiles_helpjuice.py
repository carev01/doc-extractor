"""Tests for the Helpjuice knowledge-base profile (Axcient x360Recover fix).

Helpjuice builds its TOC from per-node JSON endpoints (``/en_US/<slug>.json``)
that carry both sub-sections (``children``) and the section's articles
(``published_questions``). We walk **down** from the source's own node, which
scopes extraction to that subtree (one product out of a multi-product KB).

Hermetic: a FakeScraper serves canned JSON, no network.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import json

import pytest

from app.services.profiles.scraper import FakeScraper
from app.services.profiles.detector import detect_platform
from app.services.profiles.content_scope import scope_content_html
from app.services.profiles.helpjuice import HelpjuiceProfile

HOST = "https://help.example.com"
ROOT = f"{HOST}/en_US/100-Product-A"


def test_opts_into_raw_http():
    assert HelpjuiceProfile().content_engine == "raw_http"


def test_detects_on_helpjuice_attributes():
    html = '<aside class="custom-sidebar" data-helpjuice-element="Container"></aside>'
    assert detect_platform(html, ROOT) == "helpjuice"


def test_detect_negative_on_plain_html():
    assert HelpjuiceProfile().detect("<html><body><p>hi</p></body></html>", ROOT) is False


def test_json_url_derivation():
    P = HelpjuiceProfile
    # Source root: id-prefixed slug, /en_US/ kept and normalized.
    assert P._json_url(f"{HOST}/en_US/100-Product-A") == f"{HOST}/en_US/100-Product-A.json"
    # Child URL from the API: no /en_US/, carries ?kb_language — stripped.
    assert P._json_url(f"{HOST}/setup-guide?kb_language=en_US") == f"{HOST}/en_US/setup-guide.json"


def _scraper():
    """A 2-level KB scoped under Product-A. Product-B is a sibling and must NOT
    appear (we only walk down from the root node)."""
    root = {
        "name": "Product A",
        "children": [
            {"name": "Setup", "position": 1, "children?": True,
             "url": f"{HOST}/setup?kb_language=en_US"},
            {"name": "Release Notes", "position": 2, "children?": False,
             "url": f"{HOST}/release-notes?kb_language=en_US"},
        ],
        "published_questions": [
            {"name": "Welcome", "position": 1, "url": f"{HOST}/welcome?kb_language=en_US"},
        ],
    }
    setup = {
        "name": "Setup",
        "children": [],
        "published_questions": [
            # Out of position order on purpose — must be sorted to Install, Configure.
            {"name": "Configure", "position": 2, "url": f"{HOST}/setup/configure?kb_language=en_US"},
            {"name": "Install", "position": 1, "url": f"{HOST}/setup/install?kb_language=en_US"},
        ],
    }
    release = {
        "name": "Release Notes", "children": [],
        "published_questions": [
            {"name": "v1.0", "position": 1, "url": f"{HOST}/release-notes/v1?kb_language=en_US"},
        ],
    }
    return FakeScraper({}, raw_by_url={
        f"{HOST}/en_US/100-Product-A.json": json.dumps(root),
        f"{HOST}/en_US/setup.json": json.dumps(setup),
        f"{HOST}/en_US/release-notes.json": json.dumps(release),
    })


@pytest.mark.asyncio
async def test_builds_scoped_ordered_hierarchy():
    toc = await HelpjuiceProfile().build_toc(ROOT, _scraper())
    shape = [(e.level, e.title, e.is_article, e.url) for e in toc]
    assert shape == [
        (0, "Welcome", True, f"{HOST}/welcome"),                     # root's own article
        (0, "Setup", False, None),                                   # section = url-less header
        (1, "Install", True, f"{HOST}/setup/install"),               # sorted by position
        (1, "Configure", True, f"{HOST}/setup/configure"),
        (0, "Release Notes", False, None),
        (1, "v1.0", True, f"{HOST}/release-notes/v1"),
    ]


@pytest.mark.asyncio
async def test_article_urls_have_query_stripped():
    toc = await HelpjuiceProfile().build_toc(ROOT, _scraper())
    assert all("?" not in e.url for e in toc if e.url)


@pytest.mark.asyncio
async def test_sections_are_url_less_so_only_articles_are_scraped():
    toc = await HelpjuiceProfile().build_toc(ROOT, _scraper())
    scrapable = [e for e in toc if e.url]
    assert {e.title for e in scrapable} == {"Welcome", "Install", "Configure", "v1.0"}


@pytest.mark.asyncio
async def test_missing_node_json_degrades_gracefully():
    # The "setup" subtree JSON is absent: its header still emits, the rest of the
    # tree is intact, no exception.
    scr = _scraper()
    del scr._raw[f"{HOST}/en_US/setup.json"]
    toc = await HelpjuiceProfile().build_toc(ROOT, scr)
    titles = [e.title for e in toc]
    assert "Setup" in titles and "Install" not in titles  # header kept, children skipped
    assert "v1.0" in titles                                # sibling subtree unaffected


# ---------------------------------------------------------------------------
# Content scoping via the generic raw_http scoper (article.article + excludes)
# ---------------------------------------------------------------------------

def test_content_scopes_article_body_and_drops_byline():
    cfg = HelpjuiceProfile().content_config()
    assert cfg["includeTags"] == ["article.article"]
    html = (
        '<body><aside class="custom-sidebar"><a href="/en_US/Product-B">Product B</a></aside>'
        '<article class="article">'
        '  <header data-helpjuice-element="Article Profile Header"><h1>Install</h1>'
        '    <div data-helpjuice-element="Author Profile Header" class="author">Written By Jane</div>'
        '    <div data-helpjuice-element="Article Author Details" class="details">Updated at Jan 1</div>'
        '  </header>'
        '  <div class="fr-view"><p>Real install prose.</p></div>'
        '  <ul class="tags"><li>tag</li></ul>'
        '</article></body>'
    )
    out = scope_content_html(
        html, f"{HOST}/en_US/setup/install",
        cfg["includeTags"], cfg["excludeTags"],
    )
    assert "Real install prose" in out
    assert "Install" in out                 # title header kept
    assert "Written By Jane" not in out      # author byline dropped
    assert "Updated at Jan 1" not in out     # author details dropped
    assert "Product B" not in out            # sidebar nav not in scope
