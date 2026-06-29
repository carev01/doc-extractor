"""Scraper must inject the realm cookies (auth_state) into every Browserless
render, so authenticated JS-rendered sources (e.g. Cohesity) render logged-in
during TOC discovery — not just the raw path."""

import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from app.services.profiles.scraper import Scraper


class _RecordingBrowserless:
    def __init__(self):
        self.calls = {}

    async def render(self, url, auth_state=None):
        self.calls["render"] = auth_state
        return {}

    async def render_html(self, url, wait_selector=None, auth_state=None):
        self.calls["render_html"] = auth_state
        return ""

    async def expand_toc(self, url, section_id=None, auth_state=None):
        self.calls["expand_toc"] = auth_state
        return []

    async def expand_collapsible_sidebar(self, url, auth_state=None):
        self.calls["expand_collapsible_sidebar"] = auth_state
        return ""

    async def expand_docusaurus_sidebar(self, url, auth_state=None):
        self.calls["expand_docusaurus_sidebar"] = auth_state
        return ""

    async def warmup_render(self, url, selector=None, warmup_url=None, auth_state=None):
        self.calls["warmup_render"] = auth_state
        return {}


@pytest.fixture
def fake_browserless(monkeypatch):
    rec = _RecordingBrowserless()
    mod = types.ModuleType("app.services.browserless")
    mod.browserless_client = rec
    monkeypatch.setitem(sys.modules, "app.services.browserless", mod)
    return rec


COOKIES = [{"name": "auth_session", "value": "tok", "domain": "docs.example.com"}]


@pytest.mark.asyncio
async def test_render_methods_forward_auth_state(fake_browserless):
    s = Scraper(firecrawl=None, auth_cookies=COOKIES)
    await s.render("https://x/")
    await s.get_rendered_html("https://x/")
    await s.expand_toc("https://x/")
    await s.expand_collapsible_sidebar("https://x/")
    await s.expand_docusaurus_sidebar("https://x/")
    await s.warmup_render("https://x/")
    expected = {"cookies": COOKIES}
    for method, got in fake_browserless.calls.items():
        assert got == expected, f"{method} did not forward auth_state"


@pytest.mark.asyncio
async def test_no_auth_state_when_unauthenticated(fake_browserless):
    s = Scraper(firecrawl=None, auth_cookies=None)
    await s.render("https://x/")
    await s.expand_collapsible_sidebar("https://x/")
    assert fake_browserless.calls["render"] is None
    assert fake_browserless.calls["expand_collapsible_sidebar"] is None
