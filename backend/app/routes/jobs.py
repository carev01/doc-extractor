"""Job routes — scheduled groups of sources (like backup jobs).

A job owns a schedule and a set of sources (one job per source). Firing a job
fans out into one extraction run per source, grouped under a JobRun.
"""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.job import Job
from app.models.job_run import JobRun
from app.models.product import Product
from app.models.source import DocumentationSource
from app.models.vendor import Vendor
from app.schemas.job import (
    JobCreate, JobUpdate, JobResponse, JobList, JobSourceRef, JobRunResponse,
)
from app.services.cron import build_cron, compute_next_run
from app.services.scheduling import fan_out_job

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


async def _job_sources(db: AsyncSession, job_id: uuid.UUID) -> list[JobSourceRef]:
    rows = (
        await db.execute(
            select(
                DocumentationSource.id,
                DocumentationSource.name,
                Product.name.label("product_name"),
                Vendor.name.label("vendor_name"),
            )
            .join(Product, DocumentationSource.product_id == Product.id)
            .join(Vendor, Product.vendor_id == Vendor.id)
            .where(DocumentationSource.job_id == job_id)
            .order_by(DocumentationSource.name)
        )
    ).all()
    return [
        JobSourceRef(id=r.id, name=r.name, product_name=r.product_name, vendor_name=r.vendor_name)
        for r in rows
    ]


async def _response(db: AsyncSession, job: Job) -> JobResponse:
    sources = await _job_sources(db, job.id)
    return JobResponse(
        id=job.id, name=job.name, enabled=job.enabled,
        frequency=job.frequency, time_of_day=job.time_of_day,
        day_of_week=job.day_of_week, day_of_month=job.day_of_month,
        cron=job.cron, timezone=job.timezone,
        next_run_at=job.next_run_at, last_run_at=job.last_run_at,
        source_count=len(sources), sources=sources,
    )


async def _load(db: AsyncSession, job_id: uuid.UUID) -> Job:
    job = (await db.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


def _apply_schedule(job: Job) -> None:
    """Recompute cron + next_run_at from the job's friendly schedule fields.

    A job with no frequency (or disabled) carries no cron and no next_run_at.
    """
    if job.frequency is None:
        job.cron = None
        job.next_run_at = None
        return
    job.cron = build_cron(job.frequency, job.time_of_day or "02:00", job.day_of_week, job.day_of_month)
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
    if body.enabled and body.frequency is None:
        raise HTTPException(status_code=422, detail="An enabled job needs a frequency")
    job = Job(
        name=body.name, enabled=body.enabled, frequency=body.frequency,
        time_of_day=body.time_of_day, day_of_week=body.day_of_week,
        day_of_month=body.day_of_month, timezone=body.timezone,
    )
    _apply_schedule(job)
    db.add(job)
    await db.commit()
    await db.refresh(job)
    return await _response(db, job)


@router.get("/{job_id}", response_model=JobResponse)
async def get_job(job_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    return await _response(db, await _load(db, job_id))


@router.patch("/{job_id}", response_model=JobResponse)
async def update_job(job_id: uuid.UUID, body: JobUpdate, db: AsyncSession = Depends(get_db)):
    job = await _load(db, job_id)
    data = body.model_dump(exclude_unset=True)
    for field in ("name", "enabled", "frequency", "time_of_day", "day_of_week", "day_of_month", "timezone"):
        if field in data:
            setattr(job, field, data[field])
    if job.enabled and job.frequency is None:
        raise HTTPException(status_code=422, detail="An enabled job needs a frequency")
    # Recompute the schedule whenever any schedule-affecting field changed.
    if {"enabled", "frequency", "time_of_day", "day_of_week", "day_of_month", "timezone"} & data.keys():
        _apply_schedule(job)
    await db.commit()
    await db.refresh(job)
    return await _response(db, job)


@router.delete("/{job_id}", status_code=204)
async def delete_job(job_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    job = await _load(db, job_id)
    # ON DELETE SET NULL un-assigns the job's sources automatically.
    await db.delete(job)
    await db.commit()
    return None


@router.put("/{job_id}/sources/{source_id}", response_model=JobResponse)
async def assign_source(job_id: uuid.UUID, source_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Assign a source to this job (one job per source — reassigns if needed)."""
    job = await _load(db, job_id)
    source = (
        await db.execute(select(DocumentationSource).where(DocumentationSource.id == source_id))
    ).scalar_one_or_none()
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    source.job_id = job.id
    await db.commit()
    return await _response(db, job)


@router.delete("/{job_id}/sources/{source_id}", response_model=JobResponse)
async def unassign_source(job_id: uuid.UUID, source_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    job = await _load(db, job_id)
    source = (
        await db.execute(
            select(DocumentationSource).where(
                DocumentationSource.id == source_id,
                DocumentationSource.job_id == job_id,
            )
        )
    ).scalar_one_or_none()
    if source is None:
        raise HTTPException(status_code=404, detail="Source not assigned to this job")
    source.job_id = None
    await db.commit()
    return await _response(db, job)


@router.post("/{job_id}/run", response_model=JobRunResponse)
async def run_job(job_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Manually fire a job now — fan out into per-source runs."""
    job = await _load(db, job_id)
    job_run = await fan_out_job(db, job, trigger="manual")
    if job_run is None:
        raise HTTPException(status_code=409, detail="Job has no sources assigned")
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


def _run_response(jr: JobRun) -> JobRunResponse:
    return JobRunResponse(
        id=jr.id, job_id=jr.job_id, status=jr.status.value, trigger=jr.trigger,
        sources_total=jr.sources_total, sources_done=jr.sources_done,
        sources_failed=jr.sources_failed, created_at=jr.created_at,
        started_at=jr.started_at, completed_at=jr.completed_at,
    )
