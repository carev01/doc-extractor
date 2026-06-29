import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import httpx, pytest
import app.services.notify as notify_mod

@pytest.mark.asyncio
async def test_notify_posts_when_url_set(monkeypatch):
    seen = {}
    def handler(req):
        seen["url"] = str(req.url); seen["body"] = req.content.decode()
        return httpx.Response(200)
    monkeypatch.setattr(notify_mod.settings, "notify_webhook_url", "https://hook.test/x", raising=False)
    monkeypatch.setattr(notify_mod, "_client_factory", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await notify_mod.notify("Session expired", "Realm X expired", realm="X")
    assert seen["url"] == "https://hook.test/x"
    assert "Realm X expired" in seen["body"]

@pytest.mark.asyncio
async def test_notify_noop_when_unset(monkeypatch):
    monkeypatch.setattr(notify_mod.settings, "notify_webhook_url", "", raising=False)
    await notify_mod.notify("t", "m")   # must not raise / not post

@pytest.mark.asyncio
async def test_notify_swallows_errors(monkeypatch):
    def handler(req): raise httpx.ConnectError("down")
    monkeypatch.setattr(notify_mod.settings, "notify_webhook_url", "https://hook.test/x", raising=False)
    monkeypatch.setattr(notify_mod, "_client_factory", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await notify_mod.notify("t", "m")   # must not raise
