"""Job routes — scheduled groups of sources (like backup jobs).

A job owns a schedule and a coverage (targets / tier). Firing a job
fans out into one extraction run per source, grouped under a JobRun.
"""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authz import Principal, get_principal, require_admin
from app.core.database import get_db
from app.models.job import Job
from app.models.job_run import JobRun
from app.schemas.job import (
    JobCreate, JobUpdate, JobResponse, JobList, JobRunResponse,
)
from app.services.cron import compute_next_run, job_spec, spec_to_cron
from app.services.scheduling import fan_out_job


def _require_admin(principal: Principal = Depends(get_principal)) -> None:
    require_admin(principal)


router = APIRouter(prefix="/api/jobs", tags=["jobs"], dependencies=[Depends(_require_admin)])


async def _response(db: AsyncSession, job: Job) -> JobResponse:
    return JobResponse.model_validate(job, from_attributes=True)


async def _load(db: AsyncSession, job_id: uuid.UUID) -> Job:
    job = (await db.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


_SCHEDULE_FIELDS = (
    "schedule_kind", "time_of_day", "timezone", "weekday", "interval_months",
    "start_month", "day_of_month", "nth",
)


def _apply_schedule(job: Job) -> None:
    """Recompute cron + next_run_at from the job's schedule fields.

    A job with no schedule_kind carries no cron and no next_run_at; next_run_at
    is only set while the job is enabled.
    """
    if job.schedule_kind is None:
        job.cron = None
        job.next_run_at = None
        return
    try:
        job.cron = spec_to_cron(job_spec(job))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    job.next_run_at = (
        compute_next_run(job.cron, job.timezone, datetime.now(timezone.utc))
        if job.enabled else None
    )


@router.get("", response_model=JobList)
async def list_jobs(db: AsyncSession = Depends(get_db)):
    jobs = (await db.execute(select(Job).order_by(Job.name))).scalars().all()
    return JobList(
        jobs=[await _response(db, j) for j in jobs], total=len(jobs)
    )


@router.post("", response_model=JobResponse, status_code=201)
async def create_job(body: JobCreate, db: AsyncSession = Depends(get_db)):
    if body.enabled and body.schedule_kind is None:
        raise HTTPException(status_code=422, detail="An enabled job needs a schedule")
    job = Job(
        name=body.name, enabled=body.enabled, tier_id=body.tier_id,
        **{f: getattr(body, f) for f in _SCHEDULE_FIELDS},
    )
    _apply_schedule(job)
    db.add(job)
    await db.commit()
    await db.refresh(job)
    return await _response(db, job)


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
    return [_run_response(jr, job_name=name) for jr, name in rows]


@router.get("/{job_id}", response_model=JobResponse)
async def get_job(job_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    return await _response(db, await _load(db, job_id))


@router.patch("/{job_id}", response_model=JobResponse)
async def update_job(job_id: uuid.UUID, body: JobUpdate, db: AsyncSession = Depends(get_db)):
    job = await _load(db, job_id)
    data = body.model_dump(exclude_unset=True)
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
    return _run_response(job_run)


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
    return [_run_response(r) for r in runs]


def _run_response(jr: JobRun, job_name: str | None = None) -> JobRunResponse:
    return JobRunResponse(
        id=jr.id, job_id=jr.job_id, job_name=job_name, status=jr.status.value,
        trigger=jr.trigger, sources_total=jr.sources_total,
        sources_done=jr.sources_done, sources_failed=jr.sources_failed,
        created_at=jr.created_at, started_at=jr.started_at, completed_at=jr.completed_at,
        skipped=jr.skipped,
    )
