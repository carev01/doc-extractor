import os
import sys
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.browserless import BrowserlessClient

pytestmark = pytest.mark.asyncio


async def test_render_html_threads_auth_state(monkeypatch):
    client = BrowserlessClient(url="http://bl", token="t")
    captured = {}

    async def fake_post(code, context, target_url, client=None, **kw):
        captured["authState"] = context.get("authState")
        return {"html": "<html></html>"}

    monkeypatch.setattr(client, "_post", fake_post)
    auth = {"cookies": [{"name": "sid", "value": "x"}], "origins": []}
    await client.render_html("https://docs.x.com/a", auth_state=auth)
    assert captured["authState"] == auth


async def test_run_login_returns_result(monkeypatch):
    client = BrowserlessClient(url="http://bl", token="t")

    async def fake_post(code, context, target_url, client=None, **kw):
        return {"ok": True, "cookieCount": 12, "finalUrl": "https://docs.x.com/home"}

    monkeypatch.setattr(client, "_post", fake_post)
    out = await client.run_login("export default async () => {}", {"url": "https://x"})
    assert out["ok"] is True and out["cookieCount"] == 12
