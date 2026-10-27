"""Phase 2 of the NetBackup split: extract each new source one at a time and stop
at the first run that suggests mis-derived identity (new articles) or content
drift (updated articles), or that does not complete. Sequential on purpose — the
owner's rule is no parallel Firecrawl/Browserless load."""
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.ops.split_phase2 import extract_sequentially

pytestmark = pytest.mark.asyncio


def _api(outcomes: dict[str, dict]):
    """Fake API: each source's run is 'running' on the first poll, then *outcome*."""
    triggered: list[str] = []
    polls: dict[str, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        parts = request.url.path.strip("/").split("/")
        if request.method == "POST" and parts[:3] == ["api", "extraction", "trigger"]:
            sid = parts[3]
            triggered.append(sid)
            return httpx.Response(200, json={"run_id": f"run-{sid}", "source_id": sid,
                                             "status": "pending", "message": ""})
        if request.method == "GET" and parts[:3] == ["api", "extraction", "runs"]:
            sid = parts[3].removeprefix("run-")
            polls[sid] = polls.get(sid, 0) + 1
            if polls[sid] == 1:
                return httpx.Response(200, json={"id": parts[3], "status": "running"})
            return httpx.Response(200, json={"id": parts[3], **outcomes[sid]})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://api")
    return client, triggered


CLEAN = {"status": "completed", "articles_new": None, "articles_updated": 0}


async def test_runs_all_when_clean():
    client, triggered = _api({"a": CLEAN, "b": CLEAN, "c": CLEAN})
    runs = await extract_sequentially(client, ["a", "b", "c"], {"a": 10, "b": 10, "c": 10}, poll_s=0)
    assert triggered == ["a", "b", "c"]
    assert [r["status"] for r in runs] == ["completed"] * 3


async def test_stops_on_new_articles():
    client, triggered = _api({"a": CLEAN, "b": {**CLEAN, "articles_new": 50}, "c": CLEAN})
    runs = await extract_sequentially(client, ["a", "b", "c"], {"a": 1000, "b": 1000, "c": 1000}, poll_s=0)
    assert triggered == ["a", "b"] and len(runs) == 2


async def test_stops_on_updated_articles():
    client, triggered = _api({"a": {**CLEAN, "articles_updated": 11}, "b": CLEAN})
    await extract_sequentially(client, ["a", "b"], {"a": 1000, "b": 1000}, poll_s=0)
    assert triggered == ["a"]


async def test_small_source_tolerates_one_change():
    # threshold = max(1, round(1% of 40)) = 1: a single genuine vendor edit is fine.
    client, triggered = _api({"a": {**CLEAN, "articles_updated": 1}, "b": CLEAN})
    await extract_sequentially(client, ["a", "b"], {"a": 40, "b": 40}, poll_s=0)
    assert triggered == ["a", "b"]


async def test_stops_on_failed_run():
    client, triggered = _api({"a": {"status": "failed", "error_message": "boom"}, "b": CLEAN})
    runs = await extract_sequentially(client, ["a", "b"], {"a": 10, "b": 10}, poll_s=0)
    assert triggered == ["a"] and runs[-1]["status"] == "failed"
