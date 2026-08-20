"""The raw_http content path must thread each entry's version-independent
``topic_key`` into process_article_result — exactly like the Firecrawl-batch and
Browserless paths do.

Regression: it didn't, so every raw_http source (the majority — most vendor docs
are statically served) keyed its articles by the literal versioned URL. A version
bump then produced brand-new literal keys that matched nothing, flagging the whole
source "new" and duplicating it instead of continuing each page's history across
versions (the CommCell duplication failure mode)."""

import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.firecrawl import firecrawl_service


class _FakeCheckpoint:
    async def load_content_done(self):
        return set()

    async def add_content_done(self, urls):
        pass


class _FakeProfile:
    # A profile-supplied extractor returns the article body straight from the
    # fetched HTML (the generic selector scoper path is exercised elsewhere).
    def extract_content_html(self, raw, url):
        return "<p>Stable body.</p>"


class _FakeDB:
    async def get(self, *args, **kwargs):
        return None  # no source/realm row needed for this path


@pytest.mark.asyncio
async def test_raw_http_passes_topic_key(monkeypatch):
    captured = []

    async def fake_process(**kwargs):
        captured.append(kwargs)
        return "new"

    async def fake_fetch(url, cookies=None, retry_statuses=None, user_agent=None):
        return "<html><body><p>Stable body.</p></body></html>"

    async def no_control(*args, **kwargs):
        return None

    monkeypatch.setattr(firecrawl_service, "process_article_result", fake_process)
    monkeypatch.setattr(firecrawl_service, "fetch_raw", fake_fetch)
    monkeypatch.setattr(firecrawl_service, "_raise_if_controlled", no_control)

    page_url = "https://documentation.commvault.com/11.46/commcell-console/x.html"
    templated_key = "https://documentation.commvault.com/{version}/commcell-console/x.html"
    url_to_entry = {
        page_url: {
            "title": "X",
            "topic_key": templated_key,
            "toc_entry_id": None,
            "sort_order": 0,
        }
    }

    await firecrawl_service._scrape_via_raw_http(
        db=_FakeDB(), source_id=uuid.uuid4(), run_id=uuid.uuid4(),
        url_to_entry=url_to_entry, profile=_FakeProfile(), checkpoint=_FakeCheckpoint(),
    )

    assert len(captured) == 1
    assert captured[0]["topic_key"] == templated_key, (
        "raw_http must forward the version-independent topic_key so a version "
        "bump matches the existing article instead of duplicating it")
