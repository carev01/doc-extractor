"""Dashboard route — per-source extraction health overview."""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.article import Article
from app.models.extraction_run import ExtractionRun, RunStatus
from app.models.job import Job
from app.models.product import Product
from app.models.source import DocumentationSource, SourceStatus
from app.models.vendor import Vendor
from app.schemas.dashboard import (
    DashboardResponse, DashboardSourceRow, DashboardSummary,
)

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/sources", response_model=DashboardResponse)
async def dashboard_sources(
    stale_days: int = Query(30, ge=1),
    db: AsyncSession = Depends(get_db),
):
    now = datetime.now(timezone.utc)

    # Active article counts per source (removed excluded).
    counts: dict = {}
    for sid, n in await db.execute(
        select(Article.source_id, func.count())
        .where(Article.removed_at.is_(None))
        .group_by(Article.source_id)
    ):
        counts[sid] = n

    # Latest run per source: DISTINCT ON keeps one row per source. Order by:
    # 1. status priority (PENDING last — it has no meaningful stats yet; all other
    #    statuses including RUNNING come first), 2. started_at DESC so the most
    #    recent non-pending run wins. Bounded to one row per source.
    # NOTE: started_at has server_default=now() so it is never NULL; NULLS LAST
    # alone is not sufficient — we must explicitly de-prioritise PENDING by status.
    _pending_last = case(
        (ExtractionRun.status == RunStatus.PENDING, 1), else_=0
    )
    latest_run: dict = {}
    for run in (
        await db.execute(
            select(ExtractionRun)
            .distinct(ExtractionRun.source_id)
            .order_by(
                ExtractionRun.source_id,
                _pending_last,
                ExtractionRun.started_at.desc(),
            )
        )
    ).scalars():
        latest_run[run.source_id] = run

    rows_q = (
        select(
            DocumentationSource,
            Vendor.name.label("vendor_name"),
            Product.name.label("product_name"),
            Job.id.label("job_id"),
            Job.name.label("job_name"),
            Job.next_run_at.label("next_run_at"),
        )
        .join(Product, DocumentationSource.product_id == Product.id)
        .join(Vendor, Product.vendor_id == Vendor.id)
        .outerjoin(Job, DocumentationSource.job_id == Job.id)
        .order_by(Vendor.name, Product.name, DocumentationSource.name)
    )
    rows = (await db.execute(rows_q)).all()

    out: list[DashboardSourceRow] = []
    total = never = stale = failing = running = 0
    stale_cutoff = now - timedelta(days=stale_days)

    for src, vendor_name, product_name, job_id, job_name, next_run_at in rows:
        total += 1
        last = src.last_extracted_at
        age = int((now - last).total_seconds()) if last else None
        if last is None:
            never += 1
        elif last < stale_cutoff:
            stale += 1
        if src.status == SourceStatus.FAILED:
            failing += 1
        if src.status == SourceStatus.EXTRACTING:
            running += 1

        run = latest_run.get(src.id)
        out.append(DashboardSourceRow(
            id=src.id, name=src.name,
            vendor_name=vendor_name, product_name=product_name,
            status=src.status.value,
            last_extracted_at=last.isoformat() if last else None,
            age_seconds=age,
            article_count=counts.get(src.id, 0),
            last_run_status=run.status.value if run else None,
            last_run_new=run.articles_extracted if run else None,
            last_run_updated=run.articles_updated if run else None,
            last_run_unchanged=run.articles_unchanged if run else None,
            job_id=job_id, job_name=job_name,
            next_run_at=next_run_at.isoformat() if next_run_at else None,
        ))

    return DashboardResponse(
        summary=DashboardSummary(
            total=total, never_extracted=never, stale=stale,
            failing=failing, running=running,
        ),
        sources=out,
    )
