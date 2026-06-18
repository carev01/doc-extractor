"""Unit tests verifying that a browser User-Agent is sent on all Firecrawl
scrape requests so bot-gated sites (e.g. Confluence Cloud) render real content
instead of a JS "unsupported browser" shell.
"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.firecrawl import FirecrawlService, _BROWSER_UA


# ── helpers ──────────────────────────────────────────────────────────────────

def _fake_scrape_response(markdown="x", html="<p>x</p>"):
    """Return a mock that looks like a successful httpx Response for /v2/scrape."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"data": {"markdown": markdown, "html": html}}
    return resp


def _fake_batch_response():
    """Return a mock that looks like a successful httpx Response for /v2/batch/scrape."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"id": "batch-test-123"}
    return resp


# ── _firecrawl_request sends the UA ──────────────────────────────────────────

def test_firecrawl_request_sends_browser_ua(monkeypatch):
    """_firecrawl_request must include User-Agent: _BROWSER_UA in the POST body."""
    captured = {}

    async def fake_post(url, *, json=None, headers=None, **kwargs):
        captured["json"] = json
        return _fake_scrape_response()

    svc = FirecrawlService()
    monkeypatch.setattr(svc.client, "post", fake_post)

    async def _run():
        await svc._firecrawl_request("https://example.com/page", {"formats": ["markdown"]})

    asyncio.run(_run())

    assert "headers" in captured["json"], "POST body must contain a 'headers' key"
    assert captured["json"]["headers"]["User-Agent"] == _BROWSER_UA


def test_firecrawl_request_caller_headers_override_ua(monkeypatch):
    """If the caller already provides a 'headers' key in payload it wins over the default UA."""
    captured = {}

    async def fake_post(url, *, json=None, headers=None, **kwargs):
        captured["json"] = json
        return _fake_scrape_response()

    svc = FirecrawlService()
    monkeypatch.setattr(svc.client, "post", fake_post)

    custom_ua = "MyCustomBot/1.0"

    async def _run():
        await svc._firecrawl_request(
            "https://example.com/page",
            {"formats": ["markdown"], "headers": {"User-Agent": custom_ua}},
        )

    asyncio.run(_run())

    assert captured["json"]["headers"]["User-Agent"] == custom_ua


# ── _submit_batch sends the UA ────────────────────────────────────────────────

def test_submit_batch_sends_browser_ua(monkeypatch):
    """_submit_batch must include User-Agent: _BROWSER_UA in the POST body."""
    import uuid

    captured = {}

    async def fake_post(url, *, json=None, headers=None, **kwargs):
        captured["json"] = json
        return _fake_batch_response()

    svc = FirecrawlService()
    monkeypatch.setattr(svc.client, "post", fake_post)

    async def _run():
        await svc._submit_batch(
            ["https://example.com/page1", "https://example.com/page2"],
            source_id=uuid.uuid4(),
            content_config=None,
        )

    asyncio.run(_run())

    assert "headers" in captured["json"], "Batch POST body must contain a 'headers' key"
    assert captured["json"]["headers"]["User-Agent"] == _BROWSER_UA


def test_submit_batch_preserves_caller_headers(monkeypatch):
    """If content_config already has a 'headers' dict the UA is merged but caller wins."""
    import uuid

    captured = {}

    async def fake_post(url, *, json=None, headers=None, **kwargs):
        captured["json"] = json
        return _fake_batch_response()

    svc = FirecrawlService()
    monkeypatch.setattr(svc.client, "post", fake_post)

    custom_ua = "SpecialBot/2.0"
    content_cfg = {
        "formats": ["markdown"],
        "headers": {"User-Agent": custom_ua, "X-Extra": "yes"},
    }

    async def _run():
        await svc._submit_batch(
            ["https://example.com/page1"],
            source_id=uuid.uuid4(),
            content_config=content_cfg,
        )

    asyncio.run(_run())

    assert captured["json"]["headers"]["User-Agent"] == custom_ua
    assert captured["json"]["headers"]["X-Extra"] == "yes"


# ── constant sanity ───────────────────────────────────────────────────────────

def test_browser_ua_constant_is_chrome():
    """_BROWSER_UA must look like a Chrome browser UA (contains 'Chrome')."""
    assert "Chrome" in _BROWSER_UA
    assert "Mozilla" in _BROWSER_UA
