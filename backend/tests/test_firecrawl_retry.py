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
