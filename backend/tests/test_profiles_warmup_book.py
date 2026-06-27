"""Tests for the warmup_book profile (WAF warm-up + chapter-book; e.g. Red Hat docs).

The cold page is a WAF block, so detection is URL-based and TOC/content go
through Browserless warm-up renders. The book index's #main-content lists each
chapter as its own page; a chapter's body is <article>.

Hermetic: a FakeScraper serves a canned warm-up render of the index, no network.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from app.services.profiles.scraper import FakeScraper
from app.services.profiles.warmup_book import WarmupBookProfile, _to_multipage, _book_prefix

BOOK = "https://docs.redhat.com/en/documentation/openshift_container_platform/4.22/html/backup_and_restore/"
INDEX = BOOK + "index"
SINGLE = INDEX.replace("/html/", "/html-single/")

# #main-content outerHTML as observed: a global-nav sidebar (other books +
# this book's chapters + a sub-section #anchor), breadcrumb chrome, the chapter
# listing, and Next / duplicate Legal Notice links.
MAIN_CONTENT = f"""
<div id="main-content">
  <nav id="toc" class="table-of-contents">
    <a href="/en/documentation/openshift_container_platform/4.22/html/installing/index">Installing (other book)</a>
    <a href="{BOOK}backup-restore-overview">Backup and restore</a>
    <a href="{BOOK}oadp-application-backup-and-restore#oadp-features">OADP features (anchor)</a>
  </nav>
  <ol class="breadcrumb">
    <a href="/en">Home</a>
    <a href="/en/documentation/openshift_container_platform/4.22">OpenShift Container Platform</a>
  </ol>
  <article>
    <a href="{BOOK}index">Backup and restore</a>
    <a href="{BOOK}backup-restore-overview">Backup and restore</a>
    <a href="{BOOK}graceful-shutdown-cluster">Shutting down the cluster gracefully</a>
    <a href="{BOOK}graceful-restart-cluster">Restarting the cluster gracefully</a>
    <a href="{BOOK}hibernating-cluster">Hibernating an OpenShift Container Platform cluster</a>
    <a href="{BOOK}oadp-application-backup-and-restore">OADP Application backup and restore</a>
    <a href="{BOOK}control-plane-backup-and-restore">Control plane backup and restore</a>
    <a href="{BOOK}legal-notice">Legal Notice</a>
    <a href="{BOOK}legal-notice">Legal Notice</a>
    <a href="{BOOK}backup-restore-overview">Next</a>
  </article>
</div>
"""


def _scraper(index_url=INDEX):
    return FakeScraper({}, warmup_render_by_url={index_url: {"outerHtml": MAIN_CONTENT, "title": "Backup and restore"}})


def test_render_engine_is_browserless():
    assert WarmupBookProfile().render_engine == "browserless"


def test_detects_on_publisher_host_by_url():
    prof = WarmupBookProfile()
    # Cold HTML is a WAF "Access Denied" shell; match on the URL regardless.
    assert prof.detect("<html><title>Access Denied</title></html>", INDEX) is True
    assert prof.detect("", SINGLE) is True          # html-single form too
    assert prof.detect("", "https://example.com/docs/") is False
    assert prof.detect("", "https://docs.redhat.com/en/blog") is False  # not /documentation/


def test_html_single_normalised_to_multipage():
    assert _to_multipage(SINGLE) == INDEX
    assert _book_prefix(INDEX) == BOOK
    assert _book_prefix(BOOK + "oadp-application-backup-and-restore") == BOOK


@pytest.mark.asyncio
async def test_builds_flat_chapter_list_in_order():
    toc = await WarmupBookProfile().build_toc(INDEX, _scraper())
    assert [e.title for e in toc] == [
        "Backup and restore",
        "Shutting down the cluster gracefully",
        "Restarting the cluster gracefully",
        "Hibernating an OpenShift Container Platform cluster",
        "OADP Application backup and restore",
        "Control plane backup and restore",
        "Legal Notice",
    ]
    assert all(e.level == 0 and e.is_article for e in toc)
    oadp = next(e for e in toc if e.title.startswith("OADP"))
    assert oadp.url == BOOK + "oadp-application-backup-and-restore"


@pytest.mark.asyncio
async def test_noise_filtered_and_deduped():
    toc = await WarmupBookProfile().build_toc(INDEX, _scraper())
    urls = [e.url for e in toc]
    assert len(urls) == len(set(urls))                       # Legal Notice / Next deduped
    assert all(u.startswith(BOOK) for u in urls)             # other book + breadcrumb dropped
    assert not any("#" in u for u in urls)                   # sub-section anchor dropped
    assert INDEX not in urls                                 # book landing itself dropped
    assert not any("installing" in u for u in urls)


@pytest.mark.asyncio
async def test_html_single_source_extracts_multipage_book():
    # A source registered with the html-single URL still yields the per-chapter book.
    toc = await WarmupBookProfile().build_toc(SINGLE, _scraper(index_url=INDEX))
    assert len(toc) == 7


@pytest.mark.asyncio
async def test_missing_render_returns_empty():
    assert await WarmupBookProfile().build_toc(INDEX, FakeScraper({})) == []


def test_content_spec_targets_article_with_warmup():
    spec = WarmupBookProfile().browserless_content_spec()
    assert spec == {"selector": "article", "warmup_url": "https://docs.redhat.com/en"}
