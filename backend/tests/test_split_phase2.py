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


# Field names exactly as GET /api/extraction/runs/{id} returns them
# (routes/extraction.py) — the first version of this runner read an
# ``articles_new`` field that no endpoint returns, so its identity check could
# never fire.
RUN_FIELDS = {"status", "articles_extracted", "articles_total", "articles_updated",
              "articles_unchanged", "articles_resumed", "error_message"}


def _api(outcomes: dict[str, dict]):
    """Fake API: each source's run is 'running' on the first poll, then *outcome*
    (status only); what changed comes from the outbox counts, as in production."""
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
            body = {"id": parts[3], **{k: v for k, v in outcomes[sid].items() if k in RUN_FIELDS}}
            return httpx.Response(200, json=body)
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://api")
    return client, triggered


CLEAN = {"status": "completed", "added": 0, "updated": 0, "removed": 0}


def _counts(outcomes):
    async def change_counts(run_id: str) -> dict:
        o = outcomes[run_id.removeprefix("run-")]
        return {k: o.get(k, 0) for k in ("added", "updated", "removed")}
    return change_counts


async def _run(outcomes, ids, live):
    client, triggered = _api(outcomes)
    runs = await extract_sequentially(client, ids, live, _counts(outcomes), poll_s=0)
    return runs, triggered


async def test_runs_all_when_clean():
    runs, triggered = await _run({"a": CLEAN, "b": CLEAN, "c": CLEAN}, ["a", "b", "c"],
                                 {"a": 10, "b": 10, "c": 10})
    assert triggered == ["a", "b", "c"]
    assert [r["status"] for r in runs] == ["completed"] * 3


async def test_stops_on_added_articles():
    runs, triggered = await _run({"a": CLEAN, "b": {**CLEAN, "added": 50}, "c": CLEAN},
                                 ["a", "b", "c"], {"a": 1000, "b": 1000, "c": 1000})
    assert triggered == ["a", "b"] and len(runs) == 2


async def test_stops_on_updated_articles():
    _, triggered = await _run({"a": {**CLEAN, "updated": 11}, "b": CLEAN}, ["a", "b"],
                              {"a": 1000, "b": 1000})
    assert triggered == ["a"]


async def test_stops_on_removed_articles():
    # A sidebar that came back short on a guide under the collapse guard's
    # 20-page floor completes "clean" but tombstones the missing pages.
    _, triggered = await _run({"a": {**CLEAN, "removed": 5}, "b": CLEAN}, ["a", "b"],
                              {"a": 15, "b": 15})
    assert triggered == ["a"]


async def test_small_source_tolerates_one_change():
    # threshold = max(1, round(1% of 40)) = 1: a single genuine vendor edit is fine.
    _, triggered = await _run({"a": {**CLEAN, "updated": 1}, "b": CLEAN}, ["a", "b"],
                              {"a": 40, "b": 40})
    assert triggered == ["a", "b"]


async def test_stops_on_failed_run():
    runs, triggered = await _run({"a": {"status": "failed", "error_message": "boom"}, "b": CLEAN},
                                 ["a", "b"], {"a": 10, "b": 10})
    assert triggered == ["a"] and runs[-1]["status"] == "failed"
