"""PDF-from-URL extraction sends realm cookies so login-walled PDFs download
authenticated."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx
import pytest

import app.services.pdf_import as pdf_import


@pytest.mark.asyncio
async def test_fetch_url_bytes_sends_cookie_header(monkeypatch):
    seen = {}

    def handler(req):
        seen["cookie"] = req.headers.get("cookie")
        seen["ua"] = req.headers.get("user-agent")
        return httpx.Response(200, content=b"%PDF-1.7 ...")

    # Patch AsyncClient so the module's `async with httpx.AsyncClient(...)` uses
    # our MockTransport.
    real = httpx.AsyncClient

    def fake_client(*a, **kw):
        kw.pop("timeout", None)
        return real(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(pdf_import.httpx, "AsyncClient", fake_client)

    data = await pdf_import._fetch_url_bytes(
        "https://docs.example.com/guide.pdf",
        cookies=[{"name": "auth_session", "value": "tok"}],
    )
    assert data == b"%PDF-1.7 ..."
    assert "auth_session=tok" in (seen["cookie"] or "")
    assert seen["ua"]  # browser UA sent


@pytest.mark.asyncio
async def test_fetch_url_bytes_no_cookie_when_unauthenticated(monkeypatch):
    seen = {}

    def handler(req):
        seen["cookie"] = req.headers.get("cookie")
        return httpx.Response(200, content=b"%PDF-1.7")

    real = httpx.AsyncClient

    def fake_client(*a, **kw):
        kw.pop("timeout", None)
        return real(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(pdf_import.httpx, "AsyncClient", fake_client)

    await pdf_import._fetch_url_bytes("https://example.com/x.pdf")
    assert seen["cookie"] is None
