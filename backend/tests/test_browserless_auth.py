import os
import sys
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.browserless import BrowserlessClient

pytestmark = pytest.mark.asyncio


async def test_render_html_threads_profile(monkeypatch):
    client = BrowserlessClient(url="http://bl", token="t")
    captured = {}

    async def fake_post(code, context, target_url, client=None, profile=None, **kw):
        captured["profile"] = profile
        return {"html": "<html></html>"}

    monkeypatch.setattr(client, "_post", fake_post)
    await client.render_html("https://docs.x.com/a", profile="realm-1")
    assert captured["profile"] == "realm-1"


async def test_run_login_returns_result(monkeypatch):
    client = BrowserlessClient(url="http://bl", token="t")

    async def fake_post(code, context, target_url, client=None, profile=None, **kw):
        return {"ok": True, "cookieCount": 12, "finalUrl": "https://docs.x.com/home"}

    monkeypatch.setattr(client, "_post", fake_post)
    out = await client.run_login("export default async () => {}", {"url": "https://x"})
    assert out["ok"] is True and out["cookieCount"] == 12
