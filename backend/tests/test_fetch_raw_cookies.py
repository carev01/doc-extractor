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


@pytest.mark.asyncio
async def test_fetch_raw_cookie_survives_same_host_redirect():
    """Cookie header must be re-attached after a same-host 302 redirect.

    httpx 0.28+ explicitly strips the Cookie header when following redirects
    (it re-derives it from the client cookie jar, which is empty). This test
    confirms fetch_raw re-attaches the cookie on the second hop.
    """
    requests_seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append({
            "url": str(request.url),
            "cookie": request.headers.get("cookie"),
        })
        if len(requests_seen) == 1:
            # First hop: redirect to /p2 on the same host
            return httpx.Response(302, headers={"location": "/p2"})
        return httpx.Response(200, text="<html>page2</html>")

    svc = FirecrawlService()
    svc.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await svc.fetch_raw("https://x/p1", cookies=[{"name": "SAML", "value": "tok"}])
    await svc.client.aclose()

    assert len(requests_seen) == 2, f"Expected 2 requests, got {len(requests_seen)}"
    assert requests_seen[0]["cookie"] == "SAML=tok", "Cookie missing on first hop"
    assert requests_seen[1]["cookie"] == "SAML=tok", (
        "Cookie must be re-attached on same-host redirect; "
        f"got: {requests_seen[1]['cookie']!r}"
    )


@pytest.mark.asyncio
async def test_set_cookie_from_one_fetch_never_rides_on_the_next():
    """The shared client must not keep a cookie jar.

    A vendor answering an authenticated fetch with Set-Cookie (Securiti refreshes
    connect.sid) used to land in the long-lived client's jar, so every later
    request to that domain — including ones meant to go out logged-out — carried
    the session. Only the realm cookies a call passes explicitly may be sent.
    """
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("cookie"))
        return httpx.Response(200, text="<html>ok</html>",
                              headers={"set-cookie": "sid=fresh; Path=/"})

    svc = FirecrawlService(transport=httpx.MockTransport(handler))
    await svc.fetch_raw("https://x/a", cookies=[{"name": "SAML", "value": "tok"}])
    await svc.fetch_raw("https://x/b")
    await svc.fetch_raw("https://x/c", cookies=[{"name": "SAML", "value": "tok"}])
    assert seen == ["SAML=tok", None, "SAML=tok"]
    await svc.client.aclose()


@pytest.mark.asyncio
async def test_cookie_set_mid_redirect_chain_is_sent_on_the_next_hop():
    # The cookie-check pattern (set a cookie, redirect, insist on it) must keep
    # working for a logged-out fetch — within one call, not across calls.
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.headers.get("cookie")))
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/check",
                                                "set-cookie": "probe=1; Path=/"})
        if request.headers.get("cookie") == "probe=1":
            return httpx.Response(200, text="<html>content</html>")
        return httpx.Response(302, headers={"location": "/start"})

    svc = FirecrawlService(transport=httpx.MockTransport(handler))
    assert await svc.fetch_raw("https://x/start") == "<html>content</html>"
    assert seen == [("/start", None), ("/check", "probe=1")]
    seen.clear()
    # A new call starts with no cookies at all: it is bounced to /start and
    # earns the cookie again inside its own chain.
    assert await svc.fetch_raw("https://x/check") == "<html>content</html>"
    assert seen == [("/check", None), ("/start", None), ("/check", "probe=1")]
    await svc.client.aclose()


@pytest.mark.asyncio
async def test_logged_out_fetch_still_follows_cross_host_redirects():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "old.example":
            return httpx.Response(301, headers={"location": "https://new.example/p"})
        return httpx.Response(200, text="moved")

    svc = FirecrawlService(transport=httpx.MockTransport(handler))
    assert await svc.fetch_raw("https://old.example/p") == "moved"
    await svc.client.aclose()
