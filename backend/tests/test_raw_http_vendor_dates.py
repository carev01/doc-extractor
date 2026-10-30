"""A profile can decline the vendor's page-level revision dates.

The raw_http path hands the whole fetched document to page_dates as
``page_html`` so <head> metadata (ms.date, JSON-LD dateModified) is visible. For
a platform whose such date is a site publish stamp rather than a page revision —
AEM Guides on docs.backupexec.com stamps every page of a guide within the same
second — that would record a fabricated date under a trusted provenance label.
A profile setting ``vendor_page_dates = False`` gets only the scoped body read.
"""

import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.firecrawl import firecrawl_service

RAW = (
    '<html><head><script type="application/ld+json">'
    '{"dateModified":"2026-04-02T15:27:54.594Z"}</script></head>'
    "<body><p>Stable body.</p></body></html>"
)


class _FakeCheckpoint:
    async def load_content_done(self):
        return set()

    async def add_content_done(self, urls):
        pass


class _Profile:
    def extract_content_html(self, raw, url):
        return "<p>Stable body.</p>"


class _NoVendorDates(_Profile):
    vendor_page_dates = False


class _FakeDB:
    async def get(self, *args, **kwargs):
        return None


async def _run(monkeypatch, profile):
    captured = []

    async def fake_process(**kwargs):
        captured.append(kwargs)
        return "new"

    async def fake_fetch(url, cookies=None, retry_statuses=None, user_agent=None):
        return RAW

    async def no_control(*args, **kwargs):
        return None

    monkeypatch.setattr(firecrawl_service, "process_article_result", fake_process)
    monkeypatch.setattr(firecrawl_service, "fetch_raw", fake_fetch)
    monkeypatch.setattr(firecrawl_service, "_raise_if_controlled", no_control)
    url = "https://docs.backupexec.com/us/en/x/25-1/p.html"
    await firecrawl_service._scrape_via_raw_http(
        db=_FakeDB(), source_id=uuid.uuid4(), run_id=uuid.uuid4(),
        url_to_entry={url: {"title": "P", "topic_key": url, "toc_entry_id": None, "sort_order": 0}},
        profile=profile, checkpoint=_FakeCheckpoint(),
    )
    assert len(captured) == 1
    return captured[0]


@pytest.mark.asyncio
async def test_raw_http_passes_the_whole_page_for_dates_by_default(monkeypatch):
    assert (await _run(monkeypatch, _Profile()))["page_html"] == RAW


@pytest.mark.asyncio
async def test_a_profile_can_decline_vendor_page_dates(monkeypatch):
    assert (await _run(monkeypatch, _NoVendorDates()))["page_html"] is None


def test_aem_guides_declines_vendor_page_dates():
    from app.services.profiles.aem_guides import PROFILE
    assert PROFILE.vendor_page_dates is False
