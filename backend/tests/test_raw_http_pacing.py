import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import httpx, pytest
from app.services.firecrawl import FirecrawlService

@pytest.mark.asyncio
async def test_fetch_raw_retries_401_when_in_retry_statuses():
    calls = {"n": 0}
    def handler(req):
        calls["n"] += 1
        if calls["n"] < 2:
            return httpx.Response(401, text="nope")
        return httpx.Response(200, text="<html>ok</html>")
    svc = FirecrawlService()
    svc.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    # shrink backoff for the test if the service exposes it; otherwise this is fast enough
    svc.TRANSIENT_BACKOFF = 0.01
    out = await svc.fetch_raw("https://x/p", retry_statuses={401})
    assert "ok" in out and calls["n"] == 2
    await svc.client.aclose()

@pytest.mark.asyncio
async def test_fetch_raw_does_not_retry_401_by_default():
    calls = {"n": 0}
    def handler(req):
        calls["n"] += 1
        return httpx.Response(401, text="nope")
    svc = FirecrawlService()
    svc.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(Exception):
        await svc.fetch_raw("https://x/p")
    assert calls["n"] == 1   # 401 not retried by default
    await svc.client.aclose()
