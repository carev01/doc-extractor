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

    async def fake_fetch(url, cookies=None, retry_statuses=None, user_agent=None, session=None):
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


@pytest.mark.asyncio
async def test_raw_http_hands_each_page_its_own_session_for_its_images(monkeypatch):
    # A MindTouch page mints a guest session through its redirect chain, and its
    # images 302 to the real login without that session.
    import httpx

    captured = {}

    async def fake_process(**kwargs):
        captured[kwargs["url"]] = kwargs["image_session"]
        return "new"

    async def fake_fetch(url, cookies=None, retry_statuses=None, user_agent=None, session=None):
        session.set("authtoken", url.rsplit("/", 1)[1], domain="docs.example.com")
        return "<html><body><p>Body.</p></body></html>"

    async def no_control(*args, **kwargs):
        return None

    monkeypatch.setattr(firecrawl_service, "process_article_result", fake_process)
    monkeypatch.setattr(firecrawl_service, "fetch_raw", fake_fetch)
    monkeypatch.setattr(firecrawl_service, "_raise_if_controlled", no_control)

    pages = ["https://docs.example.com/p/a", "https://docs.example.com/p/b"]
    await firecrawl_service._scrape_via_raw_http(
        db=_FakeDB(), source_id=uuid.uuid4(), run_id=uuid.uuid4(),
        url_to_entry={u: {"title": u, "toc_entry_id": None, "sort_order": 0} for u in pages},
        profile=_FakeProfile(), checkpoint=_FakeCheckpoint(),
    )

    assert {u: s.get("authtoken") for u, s in captured.items()} == {
        "https://docs.example.com/p/a": "a", "https://docs.example.com/p/b": "b"}
    assert all(isinstance(s, httpx.Cookies) for s in captured.values())


@pytest.mark.asyncio
async def test_raw_http_shares_one_guest_session_when_the_profile_asks(monkeypatch):
    # MindTouch's guest-auth bridge refuses to mint a session for a page whose URL
    # carries an escaped character (%3F, %2C, …): 18 of x360Recover's 556 pages
    # landed on the real login. A session minted from the source root once, and
    # shared by every page and image of the run, serves them all.
    import httpx

    class _Src:
        base_url = "https://docs.example.com/Product"
        auth_realm_id = None

    class _DB:
        async def get(self, *args, **kwargs):
            return _Src()

    class _SharedProfile(_FakeProfile):
        raw_http_shared_session = True

    fetched, images = [], {}

    async def fake_fetch(url, cookies=None, retry_statuses=None, user_agent=None, session=None):
        fetched.append((url, session))
        return "<html><body><p>Body.</p></body></html>"

    async def fake_process(**kwargs):
        images[kwargs["url"]] = kwargs["image_session"]
        return "new"

    async def no_control(*args, **kwargs):
        return None

    monkeypatch.setattr(firecrawl_service, "process_article_result", fake_process)
    monkeypatch.setattr(firecrawl_service, "fetch_raw", fake_fetch)
    monkeypatch.setattr(firecrawl_service, "_raise_if_controlled", no_control)

    pages = ["https://docs.example.com/Product/a%3F", "https://docs.example.com/Product/b"]
    await firecrawl_service._scrape_via_raw_http(
        db=_DB(), source_id=uuid.uuid4(), run_id=uuid.uuid4(),
        url_to_entry={u: {"title": u, "toc_entry_id": None, "sort_order": 0} for u in pages},
        profile=_SharedProfile(), checkpoint=_FakeCheckpoint(),
    )

    assert fetched[0][0] == "https://docs.example.com/Product"   # minted first, from the root
    jars = {id(s) for _, s in fetched} | {id(s) for s in images.values()}
    assert len(jars) == 1 and isinstance(fetched[0][1], httpx.Cookies)
