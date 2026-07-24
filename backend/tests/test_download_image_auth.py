"""_download_image is auth-aware: it sends realm cookies and only persists
genuine image responses (never a 200 HTML login page from an IdP bounce)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx
import pytest

from app.services.firecrawl import FirecrawlService


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
