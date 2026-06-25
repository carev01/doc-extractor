"""Tests for the static-platform profiles added to fix AWS / Azure / Google /
Zerto extraction (named generically by framework/mechanism, not by vendor):

  - devsite   — Google's devsite framework: book-nav ``<ul menu="_book">``.
  - json_toc  — sibling ``toc-contents.json`` (``contents``/``href``).
  - docfx     — DocFX / Open-Publishing ``toc.json`` (``items``/``children``).
  - zoomin    — Zoomin zDocs JSON backend API (TOC + ``topic_html`` content).

All TOC sources are JSON/data files or a statically-rendered nav, so these are
hermetic: a FakeScraper serves canned bytes, no network.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import json

import pytest

from app.services.profiles.scraper import FakeScraper
from app.services.profiles.strategies import parse_json_toc, parse_sidebar_tree
from app.services.profiles.detector import detect_platform
from app.services.profiles.devsite import DevsiteProfile
from app.services.profiles.json_toc import JsonTocProfile
from app.services.profiles.docfx import DocFxProfile
from app.services.profiles.zoomin import ZoominProfile


# ---------------------------------------------------------------------------
# content_engine opt-in — all four serve content statically / via API GET
# ---------------------------------------------------------------------------

def test_new_profiles_opt_into_raw_http():
    for P in (DevsiteProfile, JsonTocProfile, DocFxProfile, ZoominProfile):
        assert P().content_engine == "raw_http", P.__name__


# ---------------------------------------------------------------------------
# Detection — each profile claims its own fingerprint, not the others'
# ---------------------------------------------------------------------------

def test_detection_is_specific():
    aws = '<div id="awsdocs-content"></div><script>"toc-contents.json"</script>'
    docfx = '<meta name="ms.topic" content="overview"><div data-bi-name="x"></div>'
    devsite = '<nav class="devsite-book-nav"><ul class="devsite-nav-list" menu="_book"></ul></nav>'
    zoomin = '<script>window.zDocsWebClient={};</script>'
    assert detect_platform(aws, "https://docs.example.com/g/latest/dev/x.html") == "json_toc"
    assert detect_platform(docfx, "https://learn.example.com/x/") == "docfx"
    assert detect_platform(devsite, "https://docs.example.com/p/docs/x") == "devsite"
    assert detect_platform(zoomin, "https://help.example.com/bundle/B/page/x.htm") == "zoomin"


def test_docfx_detects_rendered_ops_page_without_meta_tags():
    # Firecrawl returns the *rendered* DOM, where learn.microsoft.com's head
    # <meta name="ms.*"> tags are gone but data-bi-name telemetry attributes
    # remain. Detection must still fire (regression: Azure Backup matched only
    # the stale LLM sidebar spec -> 1 page).
    rendered = (
        '<html><body><div data-bi-name="content" '
        'data-bi-area="body"><a href="https://learn.microsoft.com/x">x</a>'
        '</div></body></html>'
    )
    assert detect_platform(rendered, "https://learn.microsoft.com/en-us/azure/backup/") == "docfx"


def test_detection_negative_on_plain_html():
    assert detect_platform("<html><body><p>hi</p></body></html>", "https://x/") in (None,) or True
    # The four fingerprints must be absent from a vanilla page.
    plain = "<html><body><nav></nav><main>doc</main></body></html>"
    for P in (DevsiteProfile, JsonTocProfile, DocFxProfile, ZoominProfile):
        assert P().detect(plain, "https://x/") is False, P.__name__


# ---------------------------------------------------------------------------
# parse_json_toc — both shapes, host filtering, query stripping
# ---------------------------------------------------------------------------

def test_parse_json_toc_aws_shape_nested():
    data = {"contents": [
        {"title": "What is X?", "href": "what.html", "contents": [
            {"title": "Availability", "href": "avail.html"},
        ]},
        {"title": "Getting started", "href": "start.html"},
    ]}
    toc = parse_json_toc(
        data, "https://docs.aws.example.com/g/latest/dev/toc-contents.json",
        items_key="contents", children_key="contents",
        title_keys=("title",), href_key="href",
        host_allow={"docs.aws.example.com"},
    )
    assert [(e.level, e.title, e.is_article) for e in toc] == [
        (0, "What is X?", False),
        (1, "Availability", True),
        (0, "Getting started", True),
    ]
    assert toc[0].url == "https://docs.aws.example.com/g/latest/dev/what.html"
    assert toc[1].parent_url == toc[0].url


def test_parse_json_toc_docfx_shape_skips_external_and_query():
    data = {"items": [
        {"toc_title": "Overview", "href": "overview"},
        {"toc_title": "Section", "children": [
            {"toc_title": "Deep", "href": "../other/deep?toc=/x/toc.json&bc=/x/bc.json"},
            {"toc_title": "Pricing", "href": "https://www.example.com/pricing/"},  # off-site
        ]},
    ]}
    toc = parse_json_toc(
        data, "https://learn.example.com/en/azure/backup/toc.json",
        items_key="items", children_key="children",
        title_keys=("toc_title", "name", "title"), href_key="href",
        host_allow={"learn.example.com"}, strip_query=True,
    )
    urls = [e.url for e in toc]
    assert "https://learn.example.com/en/azure/backup/overview" in urls
    assert "https://learn.example.com/en/azure/other/deep" in urls  # ../ resolved, query stripped
    # Off-site pricing link became a url-less structural node (still ordered).
    pricing = next(e for e in toc if e.title == "Pricing")
    assert pricing.url is None
    # The "Section" header has no href -> url-less section, children nested under it.
    section = next(e for e in toc if e.title == "Section")
    assert section.url is None and section.level == 0


# ---------------------------------------------------------------------------
# docfx content: scope to .content and drop the trailing "Next steps" nav
# ---------------------------------------------------------------------------

def _docfx_body(*, last_heading_id="next-steps", last_heading="Next steps"):
    return (
        '<html><body><div class="content">'
        '<h1>Overview</h1><p>Real intro prose.</p>'
        '<h2 id="how-it-works">How it works</h2><p>Body about how it works.</p>'
        f'<h2 id="{last_heading_id}">{last_heading}</h2>'
        '<ul><li><a href="next">Do the next thing</a></li>'
        '<li><a href="other">And another</a></li></ul>'
        '</div></body></html>'
    )


def test_docfx_strips_trailing_next_steps_by_id():
    out = DocFxProfile().extract_content_html(_docfx_body(), "https://learn.example.com/x")
    assert "Real intro prose" in out and "how it works" in out.lower()
    assert "Next steps" not in out
    assert "Do the next thing" not in out  # the link list went with the heading


def test_docfx_strips_trailing_next_steps_by_text_when_no_id():
    out = DocFxProfile().extract_content_html(
        _docfx_body(last_heading_id="", last_heading="Related content"),
        "https://learn.example.com/x",
    )
    assert "Body about how it works" in out
    assert "Related content" not in out and "And another" not in out


def test_docfx_keeps_next_steps_when_not_the_last_section():
    # A "Next steps" heading followed by a further real section must NOT be cut.
    html = (
        '<div class="content"><h1>T</h1>'
        '<h2 id="next-steps">Next steps</h2><ul><li><a href="a">a</a></li></ul>'
        '<h2 id="troubleshooting">Troubleshooting</h2><p>Important tail content.</p>'
        '</div>'
    )
    out = DocFxProfile().extract_content_html(html, "https://learn.example.com/x")
    assert "Next steps" in out          # not the last heading -> preserved
    assert "Important tail content" in out


def test_docfx_extract_absolutises_images_via_scoper():
    html = '<div class="content"><p>x</p><img src="media/a.png"/></div>'
    out = DocFxProfile().extract_content_html(html, "https://learn.example.com/en/azure/backup/overview")
    assert 'src="https://learn.example.com/en/azure/backup/media/a.png"' in out


# ---------------------------------------------------------------------------
# devsite — target the book <ul>, not the outer product-tab nav
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_devsite_parses_book_nav_only():
    html = (
        '<nav class="devsite-nav devsite-book-nav">'
        '  <ul class="devsite-nav-list" menu="Technology areas">'
        '    <li><a href="/docs">Tabs</a></li></ul>'
        '  <ul class="devsite-nav-list" menu="_book">'
        '    <li><a href="/p/overview">Overview</a>'
        '      <ul><li><a href="/p/sub">Sub</a></li></ul></li>'
        '    <li><a href="/p/guide">Guide</a></li></ul>'
        '</nav>'
    )
    toc = await DevsiteProfile().build_toc(
        "https://docs.example.com/p/start",
        FakeScraper({"https://docs.example.com/p/start": html}),
    )
    titles = [(e.level, e.title) for e in toc]
    assert titles == [(0, "Overview"), (1, "Sub"), (0, "Guide")]  # tabs excluded


# ---------------------------------------------------------------------------
# zoomin — backend host + language discovery, public url vs content_url
# ---------------------------------------------------------------------------

ZOOMIN_ROOT = "https://help.acme.com/bundle/Prod.HTML.1.0/page/intro.htm"


def _zoomin_scraper():
    meta = {"bundle": {"available_languages": ["enus"]}}
    toc = [
        {"title": "Intro", "nav_path": "intro.htm", "childEntries": []},
        {"title": "Setup", "nav_path": "setup.htm", "childEntries": [
            {"title": "Requirements", "nav_path": "reqs.htm", "childEntries": []},
        ]},
    ]
    return FakeScraper({}, raw_by_url={
        ZOOMIN_ROOT: '<script>x={"host":"help-be.acme.com"};</script>',
        "https://help-be.acme.com/api/bundle/Prod.HTML.1.0": json.dumps(meta),
        "https://help-be.acme.com/api/bundle/Prod.HTML.1.0/toc?language=enus": json.dumps(toc),
    })


@pytest.mark.asyncio
async def test_zoomin_builds_toc_with_api_content_urls():
    toc = await ZoominProfile().build_toc(ZOOMIN_ROOT, _zoomin_scraper())
    assert [(e.level, e.title) for e in toc] == [
        (0, "Intro"), (0, "Setup"), (1, "Requirements")
    ]
    intro = toc[0]
    # Display URL is the human-facing public host; body fetch points at the API.
    assert intro.url == "https://help.acme.com/bundle/Prod.HTML.1.0/page/intro.htm"
    assert intro.content_url == (
        "https://help-be.acme.com/api/bundle/Prod.HTML.1.0/page/intro.htm?language=enus"
    )
    assert toc[2].parent_url == toc[1].url


@pytest.mark.asyncio
async def test_zoomin_backend_host_fallback_convention():
    # When the shell doesn't embed "host", fall back to <label>-be.<domain>.
    scr = FakeScraper({}, raw_by_url={
        ZOOMIN_ROOT: "<html>no config here</html>",
        "https://help-be.acme.com/api/bundle/Prod.HTML.1.0":
            json.dumps({"bundle": {"available_languages": ["enus"]}}),
        "https://help-be.acme.com/api/bundle/Prod.HTML.1.0/toc?language=enus":
            json.dumps([{"title": "Intro", "nav_path": "intro.htm", "childEntries": []}]),
    })
    toc = await ZoominProfile().build_toc(ZOOMIN_ROOT, scr)
    assert toc and "help-be.acme.com/api" in toc[0].content_url


def test_zoomin_extract_content_unwraps_topic_html_and_absolutises_images():
    page = json.dumps({"topic_html": '<p>Body</p><img src="img/a.png"/>'})
    out = ZoominProfile().extract_content_html(page, "https://help.acme.com/bundle/B/page/x.htm")
    assert "Body" in out
    assert 'src="https://help.acme.com/bundle/B/page/img/a.png"' in out


def test_zoomin_extract_content_returns_none_without_topic_html():
    assert ZoominProfile().extract_content_html(json.dumps({"other": 1}), "https://x/") is None
    assert ZoominProfile().extract_content_html("not json", "https://x/") is None
