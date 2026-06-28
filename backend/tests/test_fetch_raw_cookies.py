import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx
import pytest

from app.services.firecrawl import FirecrawlService, _cookie_header


def test_cookie_header_builds_pairs():
    assert _cookie_header([{"name": "a", "value": "1"}, {"name": "b", "value": "2"}]) == "a=1; b=2"


def test_cookie_header_empty_is_none():
    assert _cookie_header([]) is None
    assert _cookie_header(None) is None


@pytest.mark.asyncio
async def test_fetch_raw_sends_cookie_header_when_given():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["cookie"] = request.headers.get("cookie")
        return httpx.Response(200, text="<html>ok</html>")

    svc = FirecrawlService()
    svc.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await svc.fetch_raw("https://x/p", cookies=[{"name": "SAML", "value": "tok"}])
    assert seen["cookie"] == "SAML=tok"
    await svc.client.aclose()


@pytest.mark.asyncio
async def test_fetch_raw_no_cookie_header_when_absent():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["cookie"] = request.headers.get("cookie")
        return httpx.Response(200, text="<html>ok</html>")

    svc = FirecrawlService()
    svc.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await svc.fetch_raw("https://x/p")
    assert seen["cookie"] is None
    await svc.client.aclose()
