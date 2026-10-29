"""Per-product schedule view: when was each product refreshed, and when next.

A refresh is a completed ``kind='extract'`` run (any trigger). A product must
never look fresher than it is, so its ``last_ok_at`` is the *least* recently OK
source, and its ``next_at`` the *latest* upcoming job among its sources.
Query count is constant in the number of products/sources.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.sql import any_of
from app.models import DocumentationSource, ExtractionRun, Job, JobRun, Product, Tier, Vendor
from app.models.extraction_run import RunStatus
from app.services.job_coverage import resolve_sources

_SKIPS_SQL = text("""
    SELECT DISTINCT ON (e->>'source_id')
           (e->>'source_id')::uuid AS source_id, e->>'reason' AS reason,
           jr.created_at AS at, j.name AS job_name
    FROM job_runs jr
    JOIN jobs j ON j.id = jr.job_id
    CROSS JOIN LATERAL jsonb_array_elements(
        CASE WHEN jsonb_typeof(jr.skipped) = 'array' THEN jr.skipped ELSE '[]'::jsonb END) e
    WHERE (e->>'source_id')::uuid = ANY(:ids)
    ORDER BY e->>'source_id', jr.created_at DESC
""")


def _has_blocked(run: ExtractionRun) -> bool:
    return isinstance(run.blocked_pending, list) and len(run.blocked_pending) > 0


def _run_event(run: ExtractionRun, job_name: str | None) -> dict:
    if run.status == RunStatus.FAILED:
        outcome = "failed"
    else:
        outcome = "warn" if _has_blocked(run) else "ok"
    return {
        "at": run.completed_at or run.started_at, "outcome": outcome,
        "trigger": run.trigger, "job_name": job_name,
        "updated": (run.articles_extracted or 0) + (run.articles_updated or 0),
        "reason": (run.error_message or "")[:300] or None if run.status == RunStatus.FAILED else None,
        "blocked": len(run.blocked_pending) if _has_blocked(run) else 0,
    }


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

    latest_run: dict[UUID, tuple[ExtractionRun, str | None]] = {}
    last_ok: dict[UUID, datetime] = {}
    skips: dict[UUID, dict] = {}
    if sids:
        E = ExtractionRun
        when = E.completed_at.desc().nullslast(), E.started_at.desc()
        for run, jname in (await db.execute(
            select(E, Job.name)
            .outerjoin(JobRun, JobRun.id == E.job_run_id)
            .outerjoin(Job, Job.id == JobRun.job_id)
            .where(any_of(E.source_id, sids), E.kind == "extract",
                   E.status.in_([RunStatus.COMPLETED, RunStatus.FAILED]))
            .distinct(E.source_id).order_by(E.source_id, *when)
        )).all():
            latest_run[run.source_id] = (run, jname)
        for run in (await db.execute(
            select(E)
            .where(any_of(E.source_id, sids), E.kind == "extract",
                   E.status == RunStatus.COMPLETED)
            .where(text("CASE WHEN jsonb_typeof(extraction_runs.blocked_pending) = 'array' "
                        "THEN jsonb_array_length(extraction_runs.blocked_pending) = 0 "
                        "ELSE true END"))
            .distinct(E.source_id).order_by(E.source_id, *when)
        )).scalars():
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

    products = []
    for pid, pname, vname, tname in rows:
        sources, job_names = [], set()
        for sid, sname in by_product.get(pid, []):
            cj = src_jobs.get(sid, [])
            job_names.update(j.name for j in cj)
            nexts = [j.next_run_at for j in cj if j.enabled and j.next_run_at]
            ev = [_run_event(*latest_run[sid])] if sid in latest_run else []
            if sid in skips:
                ev.append(skips[sid])
            sources.append({
                "source_id": sid, "source_name": sname,
                "last_ok_at": last_ok.get(sid),
                "latest": pick_latest(ev),
                "next_at": min(nexts) if nexts else None,
                "jobs": sorted(j.name for j in cj),
            })
        oks = [s["last_ok_at"] for s in sources]
        nexts = [s["next_at"] for s in sources if s["next_at"]]
        products.append({
            "product_id": pid, "product_name": pname, "vendor_name": vname,
            "tier_name": tname, "jobs": sorted(job_names),
            "source_count": len(sources),
            "last_ok_at": min(oks) if oks and all(oks) else None,
            "latest": pick_latest([s["latest"] for s in sources]),
            "next_at": max(nexts) if nexts else None,
            "scheduled_sources": len(nexts),
            "sources": sources,
        })
    return {"products": products}
