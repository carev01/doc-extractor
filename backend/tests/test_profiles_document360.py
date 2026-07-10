"""Tests for the Document360 profile.

Document360 knowledge bases (e.g. Securiti's helpcenter.securiti.ai/docs) embed
the whole nav tree as Angular TransferState JSON in a
``<script id="serverApp-state">`` tag, and server-render each article body under
``#articleContent``. The profile builds the ordered TOC from that JSON and scopes
content to the article container.

Hermetic: a FakeScraper serves the committed fixture (a compact page mirroring
the real serverApp-state schema); no network.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from app.services.profiles.scraper import FakeScraper
from app.services.profiles.detector import detect_platform
from app.services.profiles.document360 import (
    Document360Profile,
    parse_document360_nav,
)

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "platforms")
ROOT = "https://helpcenter.securiti.ai/docs"


def _fixture() -> str:
    return open(os.path.join(FIXTURE_DIR, "document360.html"), encoding="utf-8").read()


# ── Detection ──────────────────────────────────────────────────────────────

def test_document360_opts_into_raw_http():
    assert Document360Profile().content_engine == "raw_http"


def test_detects_document360_fixture():
    assert detect_platform(_fixture(), ROOT) == "document360"


def test_detects_on_banner_and_web_component():
    p = Document360Profile()
    assert p.detect("<!-- powered by Document360 -->", ROOT) is True
    assert p.detect("<d360-article id='articleContent'></d360-article>", ROOT) is True
    assert p.detect("<html>nothing here</html>", ROOT) is False


def test_document360_wins_over_zendesk_despite_zendesk_mention():
    # The fixture contains a "/docs/zendesk" link; detection must NOT route it to
    # the zendesk profile (registration order + tightened zendesk.detect).
    assert "zendesk" in _fixture().lower()
    assert detect_platform(_fixture(), ROOT) == "document360"


def test_bare_zendesk_mention_does_not_detect_as_zendesk():
    from app.services.profiles.zendesk import ZendeskProfile
    html = "<html><body><a href='/docs/zendesk'>Zendesk integration</a></body></html>"
    assert ZendeskProfile().detect(html, "https://helpcenter.securiti.ai/docs") is False


# ── TOC parsing ──────────────────────────────────────────────────────────────

def test_build_toc_orders_and_nests_the_tree():
    toc = parse_document360_nav(_fixture(), ROOT)
    rows = [(e.level, e.is_article, e.title, e.url) for e in toc]

    # Sorted by `order`: Getting Started (order 1) precedes What's New (order 2),
    # even though What's New appears... they're emitted in order.
    titles = [t for _, _, t, _ in rows]
    assert titles == [
        "Getting Started",                     # L0 page-category (categoryType 1 — has body)
        "Data Security Posture Management",    # L1 pure folder (url-less header)
        "DSPM Overview",                       # L2 article under the folder
        "Quickstart",                          # L1 article
        "What's New in Securiti",              # L0 index category (categoryType 2 — header)
        "What's New in 1.147",                 # L1 article
        "What's New in 1.146",                 # L1 article
        # "Secret Draft" is isHidden → skipped
    ]
    assert "Secret Draft" not in titles


def test_build_toc_article_vs_header_and_urls():
    toc = parse_document360_nav(_fixture(), ROOT)
    by_title = {e.title: e for e in toc}

    # A categoryType-1 "page category" has its own body → scrapable article that
    # also parents children.
    gs = by_title["Getting Started"]
    assert gs.is_article is True
    assert gs.url == "https://helpcenter.securiti.ai/docs/getting-started"
    assert gs.level == 0

    # Pure folder (slug None) is a url-less structural header.
    folder = by_title["Data Security Posture Management"]
    assert folder.is_article is False
    assert folder.url is None
    assert folder.level == 1

    # Nested article carries the correct URL and nests under Getting Started.
    art = by_title["DSPM Overview"]
    assert art.is_article is True
    assert art.url == "https://helpcenter.securiti.ai/docs/dspm-overview"
    assert art.level == 2
    assert art.parent_url == gs.url


def test_index_category_is_header_but_children_are_scraped():
    # categoryType==2 is a body-less index/landing page → emitted as a url-less
    # structural header (so it doesn't inflate the article total or waste a
    # fetch), but its child articles are still emitted as scrapable pages.
    toc = parse_document360_nav(_fixture(), ROOT)
    by_title = {e.title: e for e in toc}

    idx = by_title["What's New in Securiti"]
    assert idx.is_article is False
    assert idx.url is None

    child = by_title["What's New in 1.147"]
    assert child.is_article is True
    assert child.url == "https://helpcenter.securiti.ai/docs/what-s-new-in-1-147"


def test_build_toc_empty_when_no_state():
    assert parse_document360_nav("<html><body>no state here</body></html>", ROOT) == []


@pytest.mark.asyncio
async def test_build_toc_via_scraper():
    scraper = FakeScraper(html_by_url={}, raw_by_url={ROOT: _fixture()})
    toc = await Document360Profile().build_toc(ROOT, scraper)
    assert len(toc) == 7
    # 5 scrapable articles; 2 url-less headers (a pure folder + the ct-2 index).
    assert sum(1 for e in toc if e.is_article) == 5
    assert sum(1 for e in toc if not e.is_article) == 2


def test_backoff_attributes():
    # Gentler concurrency + retry 403 (429/5xx already retried by fetch_raw) so a
    # large Document360 KB survives Cloudflare rate-limiting.
    p = Document360Profile()
    assert p.raw_http_concurrency == 4
    assert 403 in p.raw_http_retry_statuses


# ── Content extraction ───────────────────────────────────────────────────────

def test_extract_content_scopes_to_article_and_absolutises_images():
    p = Document360Profile()
    url = "https://helpcenter.securiti.ai/docs/dspm-overview"
    body = p.extract_content_html(_fixture(), url)
    assert body is not None
    assert "DSPM Overview" in body
    assert "Sensitive Data Intelligence" in body
    # In-article chrome is stripped…
    assert "Related junk" not in body
    # …and relative image srcs are absolutised against the article URL.
    assert 'src="https://helpcenter.securiti.ai/media/diagram.png"' in body


def test_extract_content_none_when_no_article_container():
    p = Document360Profile()
    assert p.extract_content_html("<html><body><p>no article</p></body></html>", ROOT) is None
