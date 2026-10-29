"""Job routes — scheduled groups of sources (like backup jobs).

A job owns a schedule and a coverage (targets / tier). Firing a job
fans out into one extraction run per source, grouped under a JobRun.
"""

import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authz import Principal, get_principal, require_admin
from app.core.database import get_db
from app.core.sql import any_of
from app.models import AuthRealm, DocumentationSource, Product, Tier, Vendor
from app.models.job import Job
from app.models.job_run import JobRun
from app.models.job_target import JobTarget
from app.schemas.job import (
    CatalogProduct, CatalogSource, CatalogVendor, CoverageSet, JobCoverage,
    JobCreate, JobUpdate, JobResponse, JobList, JobRunResponse, JobTargetsOut,
    SchedulePreview,
)
from app.services.cron import (
    ScheduleSpec, compute_next_run, job_spec, next_runs, spec_to_cron, validate_spec,
)
from app.services.job_coverage import normalize_targets, resolve_sources
from app.services.scheduling import fan_out_job, job_run_children_summary


def _require_admin(principal: Principal = Depends(get_principal)) -> None:
    require_admin(principal)


router = APIRouter(prefix="/api/jobs", tags=["jobs"], dependencies=[Depends(_require_admin)])


async def _responses(db: AsyncSession, jobs: list[Job]) -> list[JobResponse]:
    """Build JobResponses for many jobs with a constant number of queries."""
    if not jobs:
        return []
    ids = [j.id for j in jobs]
    # Overlap needs every job's coverage, not just the listed ones.
    all_ids = list((await db.execute(select(Job.id))).scalars())
    cover = await resolve_sources(db, all_ids)
    owners: dict[uuid.UUID, int] = {}
    for sids in cover.values():
        for sid in sids:
            owners[sid] = owners.get(sid, 0) + 1

    covered = set().union(*(cover[i] for i in ids))
    info: dict[uuid.UUID, tuple[uuid.UUID, bool]] = {}
    if covered:
        for sid, pid, realm in (await db.execute(
            select(DocumentationSource.id, DocumentationSource.product_id,
                   DocumentationSource.auth_realm_id)
            .where(any_of(DocumentationSource.id, covered))
        )).all():
            info[sid] = (pid, realm is not None)

    targets: dict[uuid.UUID, JobTargetsOut] = {i: JobTargetsOut() for i in ids}
    for t in (await db.execute(
        select(JobTarget).where(any_of(JobTarget.job_id, ids))
    )).scalars():
        out = targets[t.job_id]
        if t.vendor_id:
            out.vendor_ids.append(t.vendor_id)
        elif t.product_id:
            out.product_ids.append(t.product_id)
        elif t.source_id:
            out.source_ids.append(t.source_id)

    now = datetime.now(timezone.utc)
    result = []
    for j in jobs:
        sids = cover[j.id]
        upcoming: list[datetime] = []
        if j.enabled and j.schedule_kind is not None:
            try:
                upcoming = next_runs(job_spec(j), now, 3)
            except (ValueError, TypeError):
                upcoming = []
        result.append(JobResponse(
            id=j.id, name=j.name, enabled=j.enabled, schedule_kind=j.schedule_kind,
            time_of_day=j.time_of_day, timezone=j.timezone, weekday=j.weekday,
            interval_months=j.interval_months, start_month=j.start_month,
            day_of_month=j.day_of_month, nth=j.nth, tier_id=j.tier_id, cron=j.cron,
            next_run_at=j.next_run_at, last_run_at=j.last_run_at,
            targets=targets[j.id],
            coverage=JobCoverage(
                sources=len(sids),
                products=len({info[s][0] for s in sids if s in info}),
                overlap_sources=sum(1 for s in sids if owners.get(s, 0) > 1),
                login_sources=sum(1 for s in sids if s in info and info[s][1]),
            ),
            next_runs=upcoming,
        ))
    return result


async def _response(db: AsyncSession, job: Job) -> JobResponse:
    return (await _responses(db, [job]))[0]


async def _load(db: AsyncSession, job_id: uuid.UUID) -> Job:
    job = (await db.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


async def _require_tier(db: AsyncSession, tier_id: uuid.UUID | None) -> None:
    if tier_id is not None and await db.get(Tier, tier_id) is None:
        raise HTTPException(status_code=404, detail="Tier not found")


_SCHEDULE_FIELDS = (
    "schedule_kind", "time_of_day", "timezone", "weekday", "interval_months",
    "start_month", "day_of_month", "nth",
)

# Which optional fields each kind actually uses; the rest are cleared.
_KIND_FIELDS = {
    "daily": set(),
    "weekly": {"weekday"},
    "monthly_day": {"interval_months", "start_month", "day_of_month"},
    "monthly_nth": {"interval_months", "start_month", "weekday", "nth"},
}
_OPTIONAL = ("weekday", "interval_months", "start_month", "day_of_month", "nth")


def _422(message: str):
    return HTTPException(status_code=422, detail=message)


def _next_month(tz_name: str) -> int:
    return datetime.now(ZoneInfo(tz_name)).month % 12 + 1


def _validate_loose(job: Job) -> None:
    """Range-check whatever schedule fields are present when there is no kind
    (a manual group must not persist garbage that a later kind switch trips on)."""
    if job.time_of_day is not None:
        try:
            validate_spec(ScheduleSpec(
                kind="daily", time_of_day=job.time_of_day, timezone=job.timezone))
        except ValueError as exc:
            raise _422(str(exc)) from None
    else:
        try:
            ZoneInfo(job.timezone)
        except Exception:
            raise _422(f"Unknown timezone: {job.timezone}") from None
    if job.weekday is not None and not 0 <= job.weekday <= 6:
        raise _422("Weekday must be between 0 (Sunday) and 6 (Saturday)")
    if job.interval_months is not None and job.interval_months not in (1, 2, 3, 4, 6, 12):
        raise _422("Month interval must be one of 1, 2, 3, 4, 6 or 12 (it has to divide a year evenly)")
    if job.start_month is not None and not 1 <= job.start_month <= 12:
        raise _422("Start month must be between 1 and 12")
    if job.day_of_month is not None and not 1 <= job.day_of_month <= 28:
        raise _422("Day of month must be between 1 and 28")
    if job.nth is not None and job.nth not in ("first", "last"):
        raise _422("Specify whether it is the first or the last weekday")


def _apply_schedule(job: Job) -> None:
    """Validate the schedule fields, drop the ones the kind doesn't use, default
    start_month, and recompute cron + next_run_at.

    A job with no schedule_kind carries no cron and no next_run_at; next_run_at
    is only set while the job is enabled.
    """
    if job.timezone is None:
        raise _422("Timezone is required")
    if job.schedule_kind is None:
        _validate_loose(job)
        job.cron = None
        job.next_run_at = None
        return
    keep = _KIND_FIELDS.get(job.schedule_kind, set())
    for f in _OPTIONAL:
        if f not in keep:
            setattr(job, f, None)
    if job.time_of_day is None:
        job.time_of_day = "02:00"
    if "interval_months" in keep and job.interval_months is None:
        job.interval_months = 1
    if "start_month" in keep and job.start_month is None:
        try:
            ZoneInfo(job.timezone)
        except Exception:
            raise _422(f"Unknown timezone: {job.timezone}") from None
        job.start_month = _next_month(job.timezone)
    try:
        job.cron = spec_to_cron(job_spec(job))
    except (ValueError, TypeError) as exc:
        raise _422(str(exc)) from None
    job.next_run_at = (
        compute_next_run(job.cron, job.timezone, datetime.now(timezone.utc))
        if job.enabled else None
    )


@router.get("", response_model=JobList)
async def list_jobs(db: AsyncSession = Depends(get_db)):
    jobs = (await db.execute(select(Job).order_by(Job.name))).scalars().all()
    return JobList(jobs=await _responses(db, list(jobs)), total=len(jobs))


@router.post("", response_model=JobResponse, status_code=201)
async def create_job(body: JobCreate, db: AsyncSession = Depends(get_db)):
    if body.enabled and body.schedule_kind is None:
        raise HTTPException(status_code=422, detail="An enabled job needs a schedule")
    await _require_tier(db, body.tier_id)
    job = Job(
        name=body.name, enabled=body.enabled, tier_id=body.tier_id,
        **{f: getattr(body, f) for f in _SCHEDULE_FIELDS},
    )
    _apply_schedule(job)
    db.add(job)
    await db.commit()
    await db.refresh(job)
    return await _response(db, job)


@router.post("/schedule/preview")
async def preview_schedule(body: SchedulePreview):
    """Next three fire times for a schedule, without saving anything."""
    job = Job(
        schedule_kind=body.kind, time_of_day=body.time_of_day, timezone=body.timezone,
        weekday=body.weekday, interval_months=body.interval_months,
        start_month=body.start_month, day_of_month=body.day_of_month, nth=body.nth,
        enabled=True,
    )
    _apply_schedule(job)
    return {"next_runs": next_runs(job_spec(job), datetime.now(timezone.utc), 3)}


@router.get("/catalog", response_model=list[CatalogVendor])
async def job_catalog(db: AsyncSession = Depends(get_db)):
    """Vendor > product > source tree (with each source's login realm) for the
    coverage picker."""
    rows = (await db.execute(
        select(Vendor.id, Vendor.name, Vendor.tier_id, Product.id, Product.name,
               DocumentationSource.id, DocumentationSource.name, AuthRealm.name)
        .select_from(Vendor)
        .outerjoin(Product, Product.vendor_id == Vendor.id)
        .outerjoin(DocumentationSource, DocumentationSource.product_id == Product.id)
        .outerjoin(AuthRealm, AuthRealm.id == DocumentationSource.auth_realm_id)
        .order_by(Vendor.name, Product.name, DocumentationSource.name)
    )).all()
    vendors: dict[uuid.UUID, CatalogVendor] = {}
    products: dict[uuid.UUID, CatalogProduct] = {}
    for vid, vname, tid, pid, pname, sid, sname, realm in rows:
        v = vendors.get(vid)
        if v is None:
            v = vendors[vid] = CatalogVendor(vendor_id=vid, vendor_name=vname, tier_id=tid)
        if pid is None:
            continue
        p = products.get(pid)
        if p is None:
            p = products[pid] = CatalogProduct(product_id=pid, product_name=pname)
            v.products.append(p)
        if sid is not None:
            p.sources.append(CatalogSource(source_id=sid, source_name=sname, auth_realm=realm))
    return list(vendors.values())


@router.get("/runs", response_model=list[JobRunResponse])
async def list_recent_job_runs(limit: int = 30, db: AsyncSession = Depends(get_db)):
    """Recent JobRuns across all jobs (newest first), each tagged with its job
    name — drives the Jobs view's Activity feed so a scheduled fan-out shows as
    one rolled-up unit rather than N loose extraction runs.

    Declared before ``/{job_id}`` so the literal ``/runs`` path isn't captured by
    the job-id route.
    """
    rows = (
        await db.execute(
            select(JobRun, Job.name)
            .join(Job, Job.id == JobRun.job_id)
            .order_by(JobRun.created_at.desc())
            .limit(limit)
        )
    ).all()
    summary = await job_run_children_summary(db, [jr.id for jr, _ in rows])
    return [_run_response(jr, summary, job_name=name) for jr, name in rows]


@router.get("/{job_id}", response_model=JobResponse)
async def get_job(job_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    return await _response(db, await _load(db, job_id))


@router.patch("/{job_id}", response_model=JobResponse)
async def update_job(job_id: uuid.UUID, body: JobUpdate, db: AsyncSession = Depends(get_db)):
    job = await _load(db, job_id)
    data = body.model_dump(exclude_unset=True)
    if data.get("tier_id") is not None:
        await _require_tier(db, data["tier_id"])
    for field in ("name", "enabled", "tier_id", *_SCHEDULE_FIELDS):
        if field in data:
            setattr(job, field, data[field])
    if job.enabled and job.schedule_kind is None:
        raise HTTPException(status_code=422, detail="An enabled job needs a schedule")
    # Recompute the schedule whenever any schedule-affecting field changed.
    if {"enabled", *_SCHEDULE_FIELDS} & data.keys():
        _apply_schedule(job)
    await db.commit()
    await db.refresh(job)
    return await _response(db, job)


@router.put("/{job_id}/coverage", response_model=JobResponse)
async def set_coverage(job_id: uuid.UUID, body: CoverageSet, db: AsyncSession = Depends(get_db)):
    """Replace the job's explicit targets with the normalized form of a picked
    source set. All-or-nothing: unknown ids are refused before anything changes."""
    job = await _load(db, job_id)
    ids = list(dict.fromkeys(body.source_ids))
    if ids:
        found = set((await db.execute(
            select(DocumentationSource.id).where(any_of(DocumentationSource.id, ids))
        )).scalars())
        missing = [str(i) for i in ids if i not in found]
        if missing:
            raise HTTPException(status_code=404, detail=f"Unknown source ids: {', '.join(missing)}")
    t = await normalize_targets(db, ids)
    await db.execute(delete(JobTarget).where(JobTarget.job_id == job.id))
    db.add_all(
        [JobTarget(job_id=job.id, vendor_id=i) for i in t.vendor_ids]
        + [JobTarget(job_id=job.id, product_id=i) for i in t.product_ids]
        + [JobTarget(job_id=job.id, source_id=i) for i in t.source_ids]
    )
    await db.commit()
    return await _response(db, job)


@router.delete("/{job_id}", status_code=204)
async def delete_job(job_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    job = await _load(db, job_id)
    await db.delete(job)
    await db.commit()
    return None


@router.post("/{job_id}/run", response_model=JobRunResponse)
async def run_job(job_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Manually fire a job now — fan out into per-source runs."""
    job = await _load(db, job_id)
    job_run = await fan_out_job(db, job, trigger="manual")
    if job_run is None:
        raise HTTPException(status_code=409, detail="Job covers no sources")
    await db.commit()
    await db.refresh(job_run)
    summary = await job_run_children_summary(db, [job_run.id])
    return _run_response(job_run, summary)


@router.get("/{job_id}/runs", response_model=list[JobRunResponse])
async def list_job_runs(
    job_id: uuid.UUID, limit: int = 50, db: AsyncSession = Depends(get_db)
):
    await _load(db, job_id)
    runs = (
        await db.execute(
            select(JobRun).where(JobRun.job_id == job_id)
            .order_by(JobRun.created_at.desc()).limit(limit)
        )
    ).scalars().all()
    summary = await job_run_children_summary(db, [r.id for r in runs])
    return [_run_response(r, summary) for r in runs]


def _run_response(
    jr: JobRun, summary: dict, job_name: str | None = None
) -> JobRunResponse:
    return JobRunResponse(
        id=jr.id, job_id=jr.job_id, job_name=job_name, status=jr.status.value,
        trigger=jr.trigger, sources_total=jr.sources_total,
        sources_done=jr.sources_done, sources_failed=jr.sources_failed,
        created_at=jr.created_at, started_at=jr.started_at, completed_at=jr.completed_at,
        children_paused=summary.get(jr.id, {}).get("paused", 0),
        skipped=jr.skipped or [],
    )
