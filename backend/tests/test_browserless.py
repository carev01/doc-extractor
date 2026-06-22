"""Tests for the Browserless client — payload shape and response unwrapping."""

import json

import httpx
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


# ── Transient-error retry ────────────────────────────────────────────────────

def _httpx_resp(status: int, body=None):
    return httpx.Response(status, request=httpx.Request("POST", "http://bl:3000/function"),
                          json=body if body is not None else {})


class _SeqClient:
    """Returns queued responses/exceptions in order, counting calls."""

    def __init__(self, seq):
        self._seq = list(seq)
        self.calls = 0

    async def post(self, url, headers=None, json=None):
        self.calls += 1
        item = self._seq.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def fast_client():
    c = BrowserlessClient(url="http://bl:3000", token="tok")
    c.TRANSIENT_BACKOFF = 0.0  # no real sleeping
    return c


@pytest.mark.asyncio
async def test_retries_transient_400_then_succeeds(fast_client):
    """Browserless surfaces a transient in-page failure as 400 — retry it."""
    seq = _SeqClient([_httpx_resp(400), _httpx_resp(200, {"data": {"toc": []}})])
    out = await fast_client.expand_toc("https://docs/x", section_id="nav__a", client=seq)
    assert out == []
    assert seq.calls == 2


@pytest.mark.asyncio
async def test_retries_transport_error_then_succeeds(fast_client):
    err = httpx.ConnectError("refused", request=httpx.Request("POST", "http://bl:3000/function"))
    seq = _SeqClient([err, _httpx_resp(200, {"data": {"sidebars": {"u": "<aside/>"}}})])
    out = await fast_client.gitbook_sidebars(["u"], client=seq)
    assert out == {"u": "<aside/>"}
    assert seq.calls == 2


@pytest.mark.asyncio
async def test_exhausts_retries_then_raises(fast_client):
    seq = _SeqClient([_httpx_resp(503)] * (fast_client.TRANSIENT_RETRIES + 1))
    with pytest.raises(BrowserlessError):
        await fast_client.expand_toc("https://docs/x", section_id="nav__a", client=seq)
    assert seq.calls == fast_client.TRANSIENT_RETRIES + 1


@pytest.mark.asyncio
async def test_non_transient_404_raises_without_retry(fast_client):
    seq = _SeqClient([_httpx_resp(404)])
    with pytest.raises(BrowserlessError):
        await fast_client.expand_toc("https://docs/x", section_id="nav__a", client=seq)
    assert seq.calls == 1  # 404 not in TRANSIENT_STATUS → no retry
