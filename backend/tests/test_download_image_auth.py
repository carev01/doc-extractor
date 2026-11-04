"""_download_image is auth-aware: it sends realm cookies and only persists
genuine image responses (never a 200 HTML login page from an IdP bounce)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx
import pytest

from app.services.firecrawl import FirecrawlService, _NoCookieJar


@pytest.mark.asyncio
async def test_download_image_sends_cookies_and_saves_image(tmp_path):
    seen = {}

    def handler(req):
        seen["cookie"] = req.headers.get("cookie")
        return httpx.Response(200, content=b"\x89PNG\r\n\x1a\n",
                              headers={"content-type": "image/png"})

    svc = FirecrawlService()
    svc.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fn = await svc._download_image(
        "https://docs.example.com/a.png", str(tmp_path),
        auth_cookies=[{"name": "SAML_TOKEN", "value": "tok"}],
    )
    assert fn is not None and fn.endswith(".png")
    assert "SAML_TOKEN=tok" in (seen["cookie"] or "")
    assert os.path.exists(os.path.join(str(tmp_path), fn))
    await svc.client.aclose()


@pytest.mark.asyncio
async def test_download_image_rejects_non_image_login_page(tmp_path):
    # An IdP bounce returns a 200 HTML login page — must not be saved as an image.
    def handler(req):
        return httpx.Response(200, text="<html>login</html>",
                              headers={"content-type": "text/html; charset=utf-8"})

    svc = FirecrawlService()
    svc.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fn = await svc._download_image("https://docs.example.com/a.svg", str(tmp_path))
    assert fn is None
    assert os.listdir(str(tmp_path)) == []
    await svc.client.aclose()


@pytest.mark.asyncio
async def test_download_image_filename_is_content_addressed(tmp_path):
    # The saved filename is derived from the image bytes, not a random UUID: the
    # same image resolves to the same name on every scrape (stable /media URL → no
    # phantom content change) and a different image to a different name.
    def handler(req):
        body = b"\x89PNG\r\n\x1a\n" + req.url.params.get("body", "A").encode()
        return httpx.Response(200, content=body, headers={"content-type": "image/png"})

    svc = FirecrawlService()
    svc.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    a1 = await svc._download_image("https://cdn/x.png?body=A", str(tmp_path))
    a2 = await svc._download_image("https://cdn/x.png?body=A", str(tmp_path))
    b1 = await svc._download_image("https://cdn/x.png?body=B", str(tmp_path))
    assert a1 == a2            # identical bytes → identical filename (deterministic)
    assert a1 != b1            # different bytes → different filename
    assert os.listdir(str(tmp_path)).count(a1) == 1  # no duplicate accumulation
    await svc.client.aclose()


@pytest.mark.asyncio
async def test_download_image_no_cookie_header_when_unauthenticated(tmp_path):
    seen = {}

    def handler(req):
        seen["cookie"] = req.headers.get("cookie")
        return httpx.Response(200, content=b"GIF89a", headers={"content-type": "image/gif"})

    svc = FirecrawlService()
    svc.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fn = await svc._download_image("https://cdn.example.com/a.gif", str(tmp_path))
    assert fn is not None and fn.endswith(".gif")
    assert seen["cookie"] is None
    await svc.client.aclose()


def _jarless(handler):
    # Like the service's own client: nothing a response sets is kept.
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), cookies=_NoCookieJar())


def _guest_session_site(seen_cookies=None):
    """A MindTouch-style site. A cookieless *page* request is bounced through a
    guest-auth bridge on another host, which sets a session cookie and redirects
    back. Anything else (an image, the API) can't mint one: a cookieless image
    goes to the real login page, and the API is a flat 403."""
    def handler(req):
        if seen_cookies is not None:
            seen_cookies[str(req.url)] = req.headers.get("cookie")
        has_session = "authtoken=guest" in (req.headers.get("cookie") or "")
        if req.url.host == "bridge.example.com":
            return httpx.Response(302, headers={
                "location": "https://docs.example.com/@api/authenticate?redirect="
                            + req.url.params["redirect"]})
        if req.url.host == "login.example.com":
            return httpx.Response(200, text="<html>sign in</html>",
                                  headers={"content-type": "text/html"})
        if req.url.host == "cdn.example.com":
            return httpx.Response(200, content=b"\x89PNG\r\n\x1a\nCDN",
                                  headers={"content-type": "image/png"})
        if req.url.path == "/@api/authenticate":
            return httpx.Response(302, headers={
                "location": req.url.params["redirect"],
                "set-cookie": "authtoken=guest; Domain=.docs.example.com; Path=/"})
        if req.url.path.startswith("/@api/tree"):
            if not has_session:
                return httpx.Response(403, text="access denied")
            return httpx.Response(200, text='{"page": {}}')
        if req.url.path.endswith(".png"):
            if not has_session:
                return httpx.Response(302, headers={"location": "https://login.example.com/"})
            return httpx.Response(200, content=b"\x89PNG\r\n\x1a\n",
                                  headers={"content-type": "image/png"})
        if not has_session:
            return httpx.Response(302, headers={
                "location": f"https://bridge.example.com/auth?redirect={req.url}"})
        return httpx.Response(200, text="<html>page</html>")
    return handler


IMG = "https://docs.example.com/@api/files/1/a.png"


@pytest.mark.asyncio
async def test_image_needing_a_session_fails_without_one(tmp_path):
    svc = FirecrawlService()
    svc.client = _jarless(_guest_session_site())
    assert await svc._download_image(IMG, str(tmp_path)) is None
    await svc.client.aclose()


@pytest.mark.asyncio
async def test_image_is_fetched_inside_the_session_its_page_established(tmp_path):
    svc = FirecrawlService()
    svc.client = _jarless(_guest_session_site())
    session = httpx.Cookies()
    assert await svc.fetch_raw("https://docs.example.com/Product/Page",
                               session=session) == "<html>page</html>"
    fn = await svc._download_image(IMG, str(tmp_path), page_session=session)
    assert fn is not None and fn.endswith(".png")
    await svc.client.aclose()


@pytest.mark.asyncio
async def test_page_session_never_reaches_another_host(tmp_path):
    seen: dict = {}
    svc = FirecrawlService()
    svc.client = _jarless(_guest_session_site(seen))
    session = httpx.Cookies()
    await svc.fetch_raw("https://docs.example.com/Product/Page", session=session)
    cdn = "https://cdn.example.com/shot.png"
    assert await svc._download_image(cdn, str(tmp_path), page_session=session)
    assert seen[cdn] is None
    await svc.client.aclose()


@pytest.mark.asyncio
async def test_fetch_raw_in_session_calls_the_api_inside_a_guest_session():
    svc = FirecrawlService()
    svc.client = _jarless(_guest_session_site())
    body = await svc.fetch_raw_in_session("https://docs.example.com/Product",
                                          "https://docs.example.com/@api/tree")
    assert body == '{"page": {}}'
    await svc.client.aclose()
