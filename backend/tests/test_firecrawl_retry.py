"""Tests for Firecrawl transient-error retry (_post_with_retry)."""
import sys
import os

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.services.firecrawl import FirecrawlService


def _resp(status: int):
    req = httpx.Request("POST", "http://fc/v2/batch/scrape")
    return httpx.Response(status, request=req, json={"id": "job-1"})


class _SeqClient:
    """Returns the queued responses/exceptions in order, recording call count."""

    def __init__(self, seq):
        self._seq = list(seq)
        self.calls = 0

    async def post(self, url, json=None, headers=None):
        self.calls += 1
        item = self._seq.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def get(self, url, headers=None, **kwargs):
        self.calls += 1
        item = self._seq.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def svc(monkeypatch):
    s = FirecrawlService()
    # No real backoff sleeping in tests.
    monkeypatch.setattr(s, "TRANSIENT_BACKOFF", 0.0)
    return s


@pytest.mark.asyncio
async def test_retries_transient_503_then_succeeds(svc):
    svc.client = _SeqClient([_resp(503), _resp(503), _resp(200)])
    resp = await svc._post_with_retry("http://fc/v2/batch/scrape", {"urls": []}, what="batch submit")
    assert resp.json()["id"] == "job-1"
    assert svc.client.calls == 3


@pytest.mark.asyncio
async def test_retries_transport_error_then_succeeds(svc):
    err = httpx.ConnectError("conn refused", request=httpx.Request("POST", "http://fc"))
    svc.client = _SeqClient([err, _resp(200)])
    resp = await svc._post_with_retry("http://fc/v2/batch/scrape", {}, what="batch submit")
    assert resp.status_code == 200
    assert svc.client.calls == 2


@pytest.mark.asyncio
async def test_4xx_raises_immediately_no_retry(svc):
    svc.client = _SeqClient([_resp(400)])
    with pytest.raises(httpx.HTTPStatusError):
        await svc._post_with_retry("http://fc/v2/batch/scrape", {}, what="batch submit")
    assert svc.client.calls == 1  # not retried


@pytest.mark.asyncio
async def test_exhausts_retries_then_raises(svc):
    svc.client = _SeqClient([_resp(503)] * (svc.TRANSIENT_RETRIES + 1))
    with pytest.raises(httpx.HTTPStatusError):
        await svc._post_with_retry("http://fc/v2/batch/scrape", {}, what="batch submit")
    assert svc.client.calls == svc.TRANSIENT_RETRIES + 1


# --- GET batch-status poll (the path that killed run bcb4cdac) ---


@pytest.mark.asyncio
async def test_batch_status_retries_connect_error_then_succeeds(svc):
    """A Firecrawl pod restart mid-poll (ConnectError) must be retried, not fatal."""
    err = httpx.ConnectError("All connection attempts failed",
                             request=httpx.Request("GET", "http://fc"))
    svc.client = _SeqClient([err, err, _resp(200)])
    data = await svc._get_batch_status("http://fc/v2/batch/scrape/job-1")
    assert data["id"] == "job-1"
    assert svc.client.calls == 3


@pytest.mark.asyncio
async def test_batch_status_retries_transient_503_then_succeeds(svc):
    svc.client = _SeqClient([_resp(503), _resp(200)])
    data = await svc._get_batch_status("http://fc/v2/batch/scrape/job-1")
    assert data["id"] == "job-1"
    assert svc.client.calls == 2


@pytest.mark.asyncio
async def test_get_with_retry_exhausts_then_raises(svc):
    err = httpx.ConnectError("nope", request=httpx.Request("GET", "http://fc"))
    svc.client = _SeqClient([err] * (svc.TRANSIENT_RETRIES + 1))
    with pytest.raises(httpx.ConnectError):
        await svc._get_with_retry("http://fc/v2/batch/scrape/job-1", what="batch status")
    assert svc.client.calls == svc.TRANSIENT_RETRIES + 1


# --- fetch_raw (raw_http content/TOC GETs) must retry transient failures ---


def _text_resp(status: int, text: str = "<html>ok</html>"):
    req = httpx.Request("GET", "http://site/page")
    return httpx.Response(status, request=req, text=text)


@pytest.mark.asyncio
async def test_fetch_raw_retries_transient_then_succeeds(svc):
    # A momentary 429/timeout on one page of a several-hundred-page raw_http run
    # must recover, not permanently drop the page (and trip the failure guard).
    err = httpx.ReadTimeout("timed out", request=httpx.Request("GET", "http://site/page"))
    svc.client = _SeqClient([_text_resp(503), err, _text_resp(200, "<p>body</p>")])
    out = await svc.fetch_raw("http://site/page")
    assert out == "<p>body</p>"
    assert svc.client.calls == 3


@pytest.mark.asyncio
async def test_fetch_raw_404_raises_immediately(svc):
    svc.client = _SeqClient([_text_resp(404)])
    with pytest.raises(httpx.HTTPStatusError):
        await svc.fetch_raw("http://site/missing")
    assert svc.client.calls == 1  # 404 is not transient — no retry
