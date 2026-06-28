"""_resolve_profile must fetch the detection HTML with the session for
authenticated sources, so a login-gated root (e.g. EON/Fern) is detected from
the authenticated page rather than the login page.

Firecrawl /scrape (Scraper.get_html) can't inject cookies; only get_raw does.
So authenticated sources must detect via get_raw. Hermetic: a recording Scraper.
"""

import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

import app.services.firecrawl as fc

# Fern markers so detect_platform returns "fern" from whichever fetch is used.
FERN_HTML = '<aside class="fern-sidebar-desktop"></aside><div class="fern-prose"></div>'


class _RecordScraper:
    last = None

    def __init__(self, firecrawl, checkpoint=None, auth_cookies=None):
        self.auth_cookies = auth_cookies
        self.calls = []
        _RecordScraper.last = self

    async def get_raw(self, url):
        self.calls.append(("get_raw", url))
        return FERN_HTML

    async def get_html(self, url, wait_ms=1500):
        self.calls.append(("get_html", url))
        return FERN_HTML


def _source():
    return types.SimpleNamespace(
        platform=None, base_url="https://docs.eon.io/user-guide/what-is-eon",
        profile_config=None,
    )


@pytest.mark.asyncio
async def test_authed_source_detects_via_get_raw(monkeypatch):
    monkeypatch.setattr(fc, "Scraper", _RecordScraper)
    svc = fc.FirecrawlService()
    cookies = [{"name": "fern_token", "value": "tok"}]
    profile = await svc._resolve_profile(_source(), auth_cookies=cookies)
    assert _RecordScraper.last.calls == [("get_raw", "https://docs.eon.io/user-guide/what-is-eon")]
    assert _RecordScraper.last.auth_cookies == cookies
    assert profile.name == "fern"


@pytest.mark.asyncio
async def test_unauthed_source_detects_via_get_html(monkeypatch):
    monkeypatch.setattr(fc, "Scraper", _RecordScraper)
    svc = fc.FirecrawlService()
    profile = await svc._resolve_profile(_source(), auth_cookies=None)
    assert _RecordScraper.last.calls == [("get_html", "https://docs.eon.io/user-guide/what-is-eon")]
    assert profile.name == "fern"
