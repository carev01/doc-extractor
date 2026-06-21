"""Tests for the Browserless client — payload shape and response unwrapping."""

import json

import pytest

from app.services.browserless import BrowserlessClient, BrowserlessError


class _FakeResp:
    def __init__(self, status=200, body=None, raise_http=False):
        self._body = body if body is not None else {}
        self._raise = raise_http

    def raise_for_status(self):
        if self._raise:
            import httpx
            raise httpx.HTTPStatusError("boom", request=None, response=None)

    def json(self):
        return self._body


class _FakeClient:
    def __init__(self, resp, capture):
        self._resp = resp
        self._capture = capture

    async def post(self, url, headers=None, json=None):
        self._capture["url"] = url
        self._capture["headers"] = headers
        self._capture["json"] = json
        return self._resp


@pytest.mark.asyncio
async def test_render_sends_token_and_target_url_and_unwraps_data():
    cap = {}
    resp = _FakeResp(body={"data": {"toc": [{"title": "A", "href": "x", "level": 1}], "contentHtml": "<p>hi</p>"}})
    client = BrowserlessClient(url="http://bl:3000", token="tok", wait_ms=5000)
    out = await client.render("https://help.salesforce.com/s/articleView?id=p.a.htm", client=_FakeClient(resp, cap))

    assert cap["url"] == "http://bl:3000/function"
    # Token goes in the Authorization header, never the URL/query (avoids log leak).
    assert cap["headers"] == {"Authorization": "Bearer tok"}
    assert "tok" not in cap["url"]
    assert cap["json"]["context"]["url"].endswith("id=p.a.htm")
    assert cap["json"]["context"]["waitMs"] == 5000
    assert out["toc"][0]["title"] == "A"
    assert out["contentHtml"] == "<p>hi</p>"


@pytest.mark.asyncio
async def test_render_accepts_unwrapped_body():
    """If Browserless returns the function value directly (no {data} wrapper)."""
    cap = {}
    resp = _FakeResp(body={"toc": [], "contentHtml": "<h1>x</h1>"})
    client = BrowserlessClient(url="http://bl:3000", token="")
    out = await client.render("https://x", client=_FakeClient(resp, cap))
    assert cap["headers"] is None  # no token -> no auth header
    assert out["contentHtml"] == "<h1>x</h1>"


@pytest.mark.asyncio
async def test_expand_toc_uses_long_session_timeout_and_returns_nodes():
    cap = {}
    nodes = [{"href": "a.html", "title": "A", "level": 0, "isParent": True},
             {"href": None, "title": "Cat", "level": 1, "isParent": True}]
    resp = _FakeResp(body={"data": {"toc": nodes}})
    client = BrowserlessClient(url="http://bl:3000", token="tok")
    out = await client.expand_toc("https://docs.example.com/index.html", client=_FakeClient(resp, cap))
    # Session timeout passed via ?timeout= (token still header-only).
    assert "?timeout=" in cap["url"] and "tok" not in cap["url"]
    assert cap["headers"] == {"Authorization": "Bearer tok"}
    assert cap["json"]["context"]["sectionId"] is None
    assert out == nodes


@pytest.mark.asyncio
async def test_render_raises_on_non_dict_payload():
    cap = {}
    resp = _FakeResp(body=["not", "a", "dict"])
    client = BrowserlessClient(url="http://bl:3000", token="t")
    with pytest.raises(BrowserlessError):
        await client.render("https://x", client=_FakeClient(resp, cap))
