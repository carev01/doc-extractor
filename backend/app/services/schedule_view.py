"""Per-product schedule view: when was each product refreshed, and when next.

A refresh is a completed ``kind='extract'`` run (any trigger). A product must
never look fresher than it is, so its ``last_ok_at`` is the *least* recently OK
source, and its ``next_at`` the *latest* upcoming job among its sources.
Query count is constant in the number of products/sources.
"""

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import literal_column, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.sql import any_of
from app.models import DocumentationSource, ExtractionRun, Job, JobRun, Product, Tier, Vendor
from app.models.extraction_run import RunStatus
from app.services.job_coverage import resolve_sources

# Latest skip per source from JobRun.skipped (JSONB written by fan_out_job).
# Historical rows are not trusted: a malformed ``source_id`` must never 500 the
# view, so the cast is guarded by a CASE (Postgres does not promise WHERE-clause
# evaluation order, so a filter beside the cast would not protect it). Only the
# last SKIPS_WINDOW_DAYS of JobRuns are scanned — older skips are not "latest".
SKIPS_WINDOW_DAYS = 400
_SKIPS_SQL = text(f"""
    SELECT DISTINCT ON (s.source_id)
           s.source_id, s.reason, jr.created_at AS at, j.name AS job_name
    FROM job_runs jr
    JOIN jobs j ON j.id = jr.job_id
    CROSS JOIN LATERAL jsonb_array_elements(
        CASE WHEN jsonb_typeof(jr.skipped) = 'array' THEN jr.skipped ELSE '[]'::jsonb END) e
    CROSS JOIN LATERAL (SELECT
        CASE WHEN e->>'source_id' ~*
                  '^[0-9a-f]{{8}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{12}}$'
             THEN (e->>'source_id')::uuid END AS source_id,
        e->>'reason' AS reason) s
    WHERE jr.created_at >= now() - interval '{SKIPS_WINDOW_DAYS} days'
      AND s.source_id = ANY(:ids)
    ORDER BY s.source_id, jr.created_at DESC
""")


_BLOCKED_COUNT = literal_column(
    "CASE WHEN jsonb_typeof(extraction_runs.blocked_pending) = 'array' "
    "THEN jsonb_array_length(extraction_runs.blocked_pending) ELSE 0 END"
)


def _run_event(run, job_name: str | None) -> dict:
    """``run`` is a lean row (never the ORM entity: log_text is up to 200 KB)."""
    failed = run.status == RunStatus.FAILED
    if failed:
        outcome = "failed"
    else:
        outcome = "warn" if run.blocked > 0 else "ok"
    return {
        "at": run.completed_at or run.started_at, "outcome": outcome,
        "trigger": run.trigger, "job_name": job_name,
        # A failed run's partial counters are not an update the product received.
        "updated": 0 if failed else (run.articles_extracted or 0) + (run.articles_updated or 0),
        "reason": ((run.error_message or "")[:300] or None) if failed else None,
        "blocked": run.blocked if run.blocked > 0 else 0,
    }


def _job_refs(jobs) -> list[dict]:
    """Jobs covering a source/product, as ``{name, enabled}`` sorted by name, so
    the UI can mark a disabled job instead of implying it will run."""
    return [{"name": j.name, "enabled": bool(j.enabled)}
            for j in sorted(jobs, key=lambda j: (j.name, str(j.id)))]


async def build_schedule(db: AsyncSession) -> dict:
    rows = (await db.execute(
        select(Product.id, Product.name, Vendor.name, Tier.name)
        .join(Vendor, Vendor.id == Product.vendor_id)
        .outerjoin(Tier, Tier.id == Vendor.tier_id)
        .order_by(Vendor.name, Product.name)
    )).all()
    src_rows = (await db.execute(
        select(DocumentationSource.id, DocumentationSource.name, DocumentationSource.product_id)
        .order_by(DocumentationSource.name)
    )).all()
    sids = [r[0] for r in src_rows]

    jobs = list((await db.execute(select(Job).order_by(Job.name))).scalars())
    cover = await resolve_sources(db, [j.id for j in jobs])
    src_jobs: dict[UUID, list[Job]] = {}
    for j in jobs:
        for sid in cover[j.id]:
            src_jobs.setdefault(sid, []).append(j)

    latest_run: dict[UUID, tuple] = {}
    last_ok: dict[UUID, datetime] = {}
    skips: dict[UUID, dict] = {}
    if sids:
        E = ExtractionRun
        when = E.completed_at.desc().nullslast(), E.started_at.desc()
        lean = (E.source_id, E.status, E.trigger, E.started_at, E.completed_at,
                E.articles_extracted, E.articles_updated, E.error_message,
                _BLOCKED_COUNT.label("blocked"))
        for row in (await db.execute(
            select(*lean, Job.name.label("job_name"))
            .outerjoin(JobRun, JobRun.id == E.job_run_id)
            .outerjoin(Job, Job.id == JobRun.job_id)
            .where(any_of(E.source_id, sids), E.kind == "extract",
                   E.status.in_([RunStatus.COMPLETED, RunStatus.FAILED]))
            .distinct(E.source_id).order_by(E.source_id, *when)
        )).all():
            latest_run[row.source_id] = (row, row.job_name)
        for run in (await db.execute(
            select(E.source_id, E.started_at, E.completed_at)
            .where(any_of(E.source_id, sids), E.kind == "extract",
                   E.status == RunStatus.COMPLETED)
            .where(text("CASE WHEN jsonb_typeof(extraction_runs.blocked_pending) = 'array' "
                        "THEN jsonb_array_length(extraction_runs.blocked_pending) = 0 "
                        "ELSE true END"))
            .distinct(E.source_id).order_by(E.source_id, *when)
        )).all():
            last_ok[run.source_id] = run.completed_at or run.started_at
        for r in (await db.execute(_SKIPS_SQL, {"ids": sids})).all():
            skips[r.source_id] = {
                "at": r.at, "outcome": "skipped", "trigger": None,
                "job_name": r.job_name, "updated": 0, "reason": r.reason, "blocked": 0,
            }

    by_product: dict[UUID, list] = {}
    for sid, sname, pid in src_rows:
        by_product.setdefault(pid, []).append((sid, sname))

    def pick_latest(events: list[dict]) -> dict | None:
        events = [e for e in events if e and e["at"]]
        return max(events, key=lambda e: e["at"]) if events else None

    _EPOCH = datetime.min.replace(tzinfo=timezone.utc)

    def least_fresh(sources: list[dict]) -> dict | None:
        """The product's latest event is its *least fresh* source's: never-OK
        sources first (one never attempted before one that only failed), then
        the oldest last OK. Taking the freshest source's event instead would
        make a multi-source product look fresher than it is."""
        if not sources:
            return None
        return min(sources, key=lambda s: (
            s["last_ok_at"] is not None,
            s["last_ok_at"] or _EPOCH,
            s["latest"] is not None,
            s["latest"]["at"] if s["latest"] else _EPOCH,
        ))["latest"]

    products = []
    for pid, pname, vname, tname in rows:
        sources, product_jobs = [], {}
        for sid, sname in by_product.get(pid, []):
            cj = src_jobs.get(sid, [])
            product_jobs.update((j.id, j) for j in cj)
            nexts = [j.next_run_at for j in cj if j.enabled and j.next_run_at]
            ev = [_run_event(*latest_run[sid])] if sid in latest_run else []
            if sid in skips:
                ev.append(skips[sid])
            sources.append({
                "source_id": sid, "source_name": sname,
                "last_ok_at": last_ok.get(sid),
                "latest": pick_latest(ev),
                "next_at": min(nexts) if nexts else None,
                "jobs": _job_refs(cj),
            })
        oks = [s["last_ok_at"] for s in sources]
        nexts = [s["next_at"] for s in sources if s["next_at"]]
        products.append({
            "product_id": pid, "product_name": pname, "vendor_name": vname,
            "tier_name": tname, "jobs": _job_refs(product_jobs.values()),
            "source_count": len(sources),
            "last_ok_at": min(oks) if oks and all(oks) else None,
            "latest": least_fresh(sources),
            "next_at": max(nexts) if nexts else None,
            "scheduled_sources": len(nexts),
            "sources": sources,
        })
    return {"products": products}
