"""Vendor revision dates + provenance (services/page_dates.py).

The field must stay NULL when a page declares nothing: a fabricated date is
worse than an honest null, because the provenance label invites trust. These
tests pin both halves — what we read, and what we refuse to.
"""

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.page_dates import (
    SOURCE_PAGE_MARKUP, SOURCE_VENDOR_META, extract_last_updated,
)


def _doc(head: str = "", body: str = "") -> str:
    return f"<html><head>{head}</head><body>{body}</body></html>"


# ── vendor_meta: a revision date in <head> ───────────────────────────────────

def test_reads_ms_date_from_head():
    """Microsoft Learn's editorial date. Its pages carry no <time> element, so
    this is the only signal they offer — and it was invisible while we only
    looked at the scoped article body."""
    html = _doc(head='<meta name="ms.date" content="2026-07-17T00:00:00Z">',
                body="<article><p>Body.</p></article>")
    when, source = extract_last_updated("<article><p>Body.</p></article>", html)
    assert when == datetime(2026, 7, 17, tzinfo=timezone.utc)
    assert source == SOURCE_VENDOR_META


def test_reads_opengraph_modified_time():
    html = _doc(head='<meta property="article:modified_time" content="2026-05-02T09:30:00Z">')
    when, source = extract_last_updated(None, html)
    assert when == datetime(2026, 5, 2, 9, 30, tzinfo=timezone.utc)
    assert source == SOURCE_VENDOR_META


def test_meta_precedence_prefers_the_cross_vendor_standard():
    """article:modified_time outranks ms.date when a page somehow has both."""
    html = _doc(head=(
        '<meta name="ms.date" content="2026-01-01T00:00:00Z">'
        '<meta property="article:modified_time" content="2026-06-06T12:00:00Z">'
    ))
    when, source = extract_last_updated(None, html)
    assert when == datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc)
    assert source == SOURCE_VENDOR_META


def test_bare_date_parses_as_midnight():
    html = _doc(head='<meta name="dcterms.modified" content="2026-02-11">')
    when, source = extract_last_updated(None, html)
    assert when == datetime(2026, 2, 11, 0, 0)
    assert source == SOURCE_VENDOR_META


# ── vendor_meta: schema.org dateModified in JSON-LD ──────────────────────────

def test_reads_json_ld_date_modified():
    """The Veeam Help Center publishes its revision date only here — no meta tag,
    no <time> element — so 34 sources read as undated without this."""
    html = _doc(head=(
        '<script type="application/ld+json">'
        '{"@context":"https://schema.org","@type":"TechArticle",'
        '"dateModified":"2026-07-21T08:00:00+08:00"}'
        "</script>"
    ))
    when, source = extract_last_updated(None, html)
    assert when.year == 2026 and when.month == 7 and when.day == 21
    assert source == SOURCE_VENDOR_META


def test_finds_date_modified_nested_in_a_graph():
    """schema.org blocks are usually wrapped in @graph or under mainEntity, so
    the value is not at the top level."""
    html = _doc(head=(
        '<script type="application/ld+json">'
        '{"@graph":[{"@type":"WebSite"},'
        '{"@type":"WebPage","mainEntity":{"dateModified":"2026-02-14T10:00:00Z"}}]}'
        "</script>"
    ))
    when, source = extract_last_updated(None, html)
    assert when == datetime(2026, 2, 14, 10, 0, tzinfo=timezone.utc)
    assert source == SOURCE_VENDOR_META


def test_a_malformed_json_ld_block_does_not_hide_a_later_good_one():
    html = _doc(head=(
        '<script type="application/ld+json">{ not json ,,</script>'
        '<script type="application/ld+json">{"dateModified":"2026-06-01"}</script>'
    ))
    when, source = extract_last_updated(None, html)
    assert when == datetime(2026, 6, 1, 0, 0)
    assert source == SOURCE_VENDOR_META


def test_meta_outranks_json_ld():
    html = _doc(head=(
        '<meta property="article:modified_time" content="2026-08-08T00:00:00Z">'
        '<script type="application/ld+json">{"dateModified":"2020-01-01"}</script>'
    ))
    when, _ = extract_last_updated(None, html)
    assert when == datetime(2026, 8, 8, tzinfo=timezone.utc)


def test_json_ld_without_a_date_is_ignored():
    html = _doc(head=(
        '<script type="application/ld+json">'
        '{"@type":"BreadcrumbList","itemListElement":[]}</script>'
    ))
    assert extract_last_updated(None, html) == (None, None)


# ── page_markup: a <time datetime> in the body ───────────────────────────────

def test_reads_time_element_from_the_body():
    """What the five currently-dated vendors (Gearset, Druva, Trilio, GRAX,
    Flosum) emit. Structured markup, hence its own tier — not `page_text`,
    which would mean a date scraped out of prose."""
    body = '<article>Updated <time datetime="2026-08-21T10:00:00Z">Aug 21</time></article>'
    when, source = extract_last_updated(body, _doc(body=body))
    assert when == datetime(2026, 8, 21, 10, 0, tzinfo=timezone.utc)
    assert source == SOURCE_PAGE_MARKUP


def test_head_metadata_outranks_a_body_time_element():
    body = '<article><time datetime="2026-01-01T00:00:00Z">old</time></article>'
    html = _doc(head='<meta name="ms.date" content="2026-09-09T00:00:00Z">', body=body)
    when, source = extract_last_updated(body, html)
    assert when == datetime(2026, 9, 9, tzinfo=timezone.utc)
    assert source == SOURCE_VENDOR_META


def test_falls_back_to_the_body_when_no_whole_document_is_supplied():
    """The render paths pass the full page as the body, so a caller that gives
    only one string must still get both tiers checked against it."""
    html = _doc(head='<meta name="ms.date" content="2026-04-04T00:00:00Z">')
    when, source = extract_last_updated(html, None)
    assert when == datetime(2026, 4, 4, tzinfo=timezone.utc)
    assert source == SOURCE_VENDOR_META


# ── What we refuse to read ───────────────────────────────────────────────────

def test_no_signal_yields_null_not_a_guess():
    """AWS docs: no meta date, no <time> element. Measured. The field must stay
    null rather than fall back to a crawl time or an HTTP header."""
    body = "<article><h1>What is AWS Backup</h1><p>Prose only.</p></article>"
    assert extract_last_updated(body, _doc(body=body)) == (None, None)


def test_unparseable_dates_are_dropped_not_coerced():
    for junk in ("last Tuesday", "", "   ", "2026-13-45", "N/A"):
        html = _doc(head=f'<meta name="ms.date" content="{junk}">')
        assert extract_last_updated(None, html) == (None, None), junk


def test_a_malformed_meta_falls_through_to_the_body():
    """An unparseable head date must not shadow a good body date."""
    body = '<article><time datetime="2026-03-03T00:00:00Z">Mar 3</time></article>'
    html = _doc(head='<meta name="ms.date" content="whenever">', body=body)
    when, source = extract_last_updated(body, html)
    assert when == datetime(2026, 3, 3, tzinfo=timezone.utc)
    assert source == SOURCE_PAGE_MARKUP


def test_empty_input_is_safe():
    assert extract_last_updated(None, None) == (None, None)
    assert extract_last_updated("", "") == (None, None)


def test_http_last_modified_is_not_consulted():
    """Deliberately unimplemented: measured returning the *current day* for AWS
    docs (CDN cache behaviour), and disagreeing with Microsoft's own page date by
    four months. A header is not an input to this function at all — the only way
    it could creep in is a future signature change, which this pins."""
    import inspect
    params = set(inspect.signature(extract_last_updated).parameters)
    assert params == {"scoped_html", "page_html"}
