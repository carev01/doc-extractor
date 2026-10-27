"""One-off, phase 2 of the NetBackup split: first extraction of each new source.

Runs one source at a time (the owner's rule: no parallel Firecrawl/Browserless
load) and stops at the first run that does not look like a pure re-link:
``articles_new`` above the threshold means identity was mis-derived (the run
would duplicate); ``articles_updated`` above it means content-hash drift (every
page would archive a version and move ``content_changed_at``). Either way, stop
and investigate before the next source. Live counts come from the database —
``/api/articles`` totals include removed rows — so this runs inside the worker pod.

Design: docs/superpowers/specs/2026-10-27-netbackup-source-split-design.md.
"""
import asyncio
import os
from typing import Mapping, Sequence

import httpx

_TERMINAL = {"completed", "failed", "cancelled"}
# A paused run will not finish on its own; report it rather than poll forever.
_STOP_STATUSES = {"failed", "cancelled", "paused"}


def _stopped(run: dict, live: int, ratio: float) -> str | None:
    threshold = max(1, round(ratio * live))
    if run.get("status") in _STOP_STATUSES:
        return f"status {run.get('status')}: {run.get('error_message')}"
    for field in ("articles_new", "articles_updated"):
        n = run.get(field) or 0
        if n > threshold:
            return f"{field}={n} > {threshold} (1% of {live} live)"
    return None


async def extract_sequentially(
    client: httpx.AsyncClient, source_ids: Sequence[str], live_counts: Mapping[str, int],
    *, ratio: float = 0.01, poll_s: float = 30.0,
) -> list[dict]:
    """Trigger and wait for each source in order; return the runs that ran,
    ending with the one that tripped the stop rule, if any."""
    runs: list[dict] = []
    for sid in source_ids:
        resp = await client.post(f"/api/extraction/trigger/{sid}")
        resp.raise_for_status()
        run_id = resp.json()["run_id"]
        while True:
            run = (await client.get(f"/api/extraction/runs/{run_id}")).raise_for_status().json()
            if run.get("status") in _TERMINAL or run.get("status") == "paused":
                break
            await asyncio.sleep(poll_s)
        runs.append(run)
        reason = _stopped(run, live_counts[sid], ratio)
        print(f"{sid}: {run.get('status')} total={run.get('articles_total')} "
              f"new={run.get('articles_new')} updated={run.get('articles_updated')} "
              f"unchanged={run.get('articles_unchanged')}" + (f"  STOP: {reason}" if reason else ""),
              flush=True)
        if reason:
            break
    return runs


async def main() -> None:
    from sqlalchemy import func, select

    from app.core.database import async_session
    from app.models import Article, DocumentationSource
    from app.ops.netbackup_split import DEFAULT_SOURCE_ID

    async with async_session() as db:
        original = await db.get(DocumentationSource, DEFAULT_SOURCE_ID)
        product_id = original.product_id if original else None
        rows = (await db.execute(
            select(DocumentationSource.id, func.count(Article.id))
            .outerjoin(Article, (Article.source_id == DocumentationSource.id) & Article.removed_at.is_(None))
            .where(DocumentationSource.product_id == product_id, DocumentationSource.id != DEFAULT_SOURCE_ID)
            .group_by(DocumentationSource.id).order_by(func.count(Article.id))
        )).all()
    counts = {str(sid): n for sid, n in rows}
    print(f"{len(counts)} sources, {sum(counts.values())} live articles", flush=True)
    headers = {"X-API-Key": os.environ["DOCEXTRACTOR_API_KEY"]}
    base = os.environ.get("DOCEXTRACTOR_API_URL", "http://docextractor-backend:8000")
    async with httpx.AsyncClient(base_url=base, headers=headers, timeout=60) as client:
        await extract_sequentially(client, list(counts), counts)


if __name__ == "__main__":
    asyncio.run(main())
