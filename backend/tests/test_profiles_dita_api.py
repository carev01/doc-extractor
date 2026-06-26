"""Tests for the dita_api profile (DITA TOC/content API; e.g. IBM Documentation).

The platform is a JS shell; the tree comes from a toc API and bodies from a
content API. We build the ordered tree from the toc JSON (children key
``topics``) and carry the content-API URL per entry in ``content_url``, distinct
from the human-facing ``?topic=`` display URL.

Hermetic: a FakeScraper serves canned toc JSON, no network.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import json

import pytest

from app.services.profiles.scraper import FakeScraper
from app.services.profiles.detector import detect_platform
from app.services.profiles.content_scope import scope_content_html
from app.services.profiles.dita_api import DitaApiProfile

ROOT = "https://www.ibm.com/docs/en/spfd/8.2.1"
TOC_API = "https://www.ibm.com/docs/api/v1/toc/spfd/8.2.1?lang=en"


def test_opts_into_raw_http():
    assert DitaApiProfile().content_engine == "raw_http"


def test_detects_on_docs_url():
    # Works off the URL even though the page is an empty JS shell.
    assert detect_platform("<html><body></body></html>", ROOT) == "dita_api"


def test_detects_on_marker_when_off_host():
    assert DitaApiProfile().detect('<div class="ibmdocs-app"></div>', "https://x/") is True


def test_detect_negative_on_plain_html():
    assert DitaApiProfile().detect("<html><body><p>hi</p></body></html>", "https://x/") is False


def _scraper():
    toc = {
        "_id": "x",
        "toc": {
            "label": "IBM Storage Protect for Databases",
            "href": "SSER7G_8.2.1",            # product root: no .html
            "topicId": "sser7g_821",
            "topics": [
                {"label": "Welcome", "topicId": "welcome",
                 "href": "SSER7G_8.2.1/landing.html"},
                {"label": "Data Protection for SQL", "topicId": "sql",
                 "href": "SSER7G_8.2.1/db-sql/overview.html", "topics": [
                     {"label": "Getting Started", "topicId": "getting-started",
                      "href": "SSER7G_8.2.1/db-sql/start.html"},
                 ]},
                {"label": "Section Only", "topics": [   # no href -> url-less header
                     {"label": "Child", "topicId": "child",
                      "href": "SSER7G_8.2.1/db-sql/child.html"},
                ]},
            ],
        },
    }
    return FakeScraper({}, raw_by_url={TOC_API: json.dumps(toc)})


@pytest.mark.asyncio
async def test_builds_ordered_tree_skipping_product_root_wrapper():
    toc = await DitaApiProfile().build_toc(ROOT, _scraper())
    shape = [(e.level, e.title, e.is_article) for e in toc]
    assert shape == [
        (0, "Welcome", True),
        (0, "Data Protection for SQL", True),
        (1, "Getting Started", True),
        (0, "Section Only", False),   # href-less node -> structural header
        (1, "Child", True),
    ]


@pytest.mark.asyncio
async def test_display_url_and_content_url_split():
    toc = await DitaApiProfile().build_toc(ROOT, _scraper())
    start = next(e for e in toc if e.title == "Getting Started")
    # Human-facing URL uses the topicId.
    assert start.url == "https://www.ibm.com/docs/en/spfd/8.2.1?topic=getting-started"
    # Body fetch hits the content API with the internal href.
    assert start.content_url == (
        "https://www.ibm.com/docs/api/v1/content/SSER7G_8.2.1/db-sql/start.html"
        "?parsebody=true&lang=en"
    )


@pytest.mark.asyncio
async def test_only_topic_pages_are_scrapable():
    toc = await DitaApiProfile().build_toc(ROOT, _scraper())
    scrapable = {e.title for e in toc if e.url}
    assert "Section Only" not in scrapable           # url-less header
    assert scrapable == {"Welcome", "Data Protection for SQL", "Getting Started", "Child"}


@pytest.mark.asyncio
async def test_missing_toc_returns_empty():
    assert await DitaApiProfile().build_toc(ROOT, FakeScraper({})) == []


def test_content_scopes_article_and_drops_breadcrumb():
    cfg = DitaApiProfile().content_config()
    assert cfg["includeTags"] == ["article"]
    html = (
        '<html><body><nav>site nav</nav>'
        '<article role="article">'
        '  <div class="familylinks"><div class="parentlink">Parent topic: SQL</div></div>'
        '  <h1>Getting Started</h1>'
        '  <div class="body"><p>Real DITA prose here.</p></div>'
        '</article></body></html>'
    )
    out = scope_content_html(
        html, "https://www.ibm.com/docs/api/v1/content/SSER7G_8.2.1/db-sql/start.html",
        cfg["includeTags"], cfg["excludeTags"],
    )
    assert "Real DITA prose here." in out
    assert "Getting Started" in out          # title kept
    assert "Parent topic: SQL" not in out    # breadcrumb dropped
    assert "site nav" not in out             # outside scope
