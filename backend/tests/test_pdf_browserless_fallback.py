"""acquire_pdf falls back to a real-Chrome (Browserless) download when the plain
HTTP client gets a non-PDF (CDN bot-shield shell) or errors."""

import os
import sys
import types
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx
import pytest

import app.services.pdf_import as pdf_import

PDF = b"%PDF-1.4 real pdf bytes"
SHELL = b"<!DOCTYPE html><html>Sign in to continue</html>"


def _src():
    return types.SimpleNamespace(id=uuid.uuid4(), base_url="https://docs.example.com/g.pdf")


def test_looks_like_pdf():
    assert pdf_import._looks_like_pdf(PDF) is True
    assert pdf_import._looks_like_pdf(SHELL) is False
    assert pdf_import._looks_like_pdf(b"") is False
    assert pdf_import._looks_like_pdf(None) is False


@pytest.mark.asyncio
async def test_no_fallback_when_direct_returns_pdf(monkeypatch):
    called = {"browser": False}

    async def direct(url, cookies=None):
        return PDF

    async def browser(url, cookies):
        called["browser"] = True
        return PDF

    monkeypatch.setattr(pdf_import, "_fetch_url_bytes", direct)
    monkeypatch.setattr(pdf_import, "_fetch_url_bytes_via_browser", browser)
    data, digest = await pdf_import.acquire_pdf(_src(), auth_cookies=[{"name": "x", "value": "y"}])
    assert data == PDF
    assert called["browser"] is False  # fast path, no Browserless


@pytest.mark.asyncio
async def test_fallback_when_direct_returns_shell(monkeypatch):
    seen = {}

    async def direct(url, cookies=None):
        return SHELL  # CDN served the login shell to the HTTP client

    async def browser(url, cookies):
        seen["cookies"] = cookies
        return PDF

    monkeypatch.setattr(pdf_import, "_fetch_url_bytes", direct)
    monkeypatch.setattr(pdf_import, "_fetch_url_bytes_via_browser", browser)
    cookies = [{"name": "auth_session", "value": "tok"}]
    data, _ = await pdf_import.acquire_pdf(_src(), auth_cookies=cookies)
    assert data == PDF
    assert seen["cookies"] == cookies


@pytest.mark.asyncio
async def test_fallback_when_direct_errors(monkeypatch):
    async def direct(url, cookies=None):
        raise httpx.ConnectError("ssl record layer failure")

    async def browser(url, cookies):
        return PDF

    monkeypatch.setattr(pdf_import, "_fetch_url_bytes", direct)
    monkeypatch.setattr(pdf_import, "_fetch_url_bytes_via_browser", browser)
    data, _ = await pdf_import.acquire_pdf(_src())
    assert data == PDF


@pytest.mark.asyncio
async def test_raises_when_both_fail(monkeypatch):
    async def direct(url, cookies=None):
        return SHELL

    async def browser(url, cookies):
        raise RuntimeError("browserless down")

    monkeypatch.setattr(pdf_import, "_fetch_url_bytes", direct)
    monkeypatch.setattr(pdf_import, "_fetch_url_bytes_via_browser", browser)
    with pytest.raises(pdf_import.PdfAcquireError):
        await pdf_import.acquire_pdf(_src())
