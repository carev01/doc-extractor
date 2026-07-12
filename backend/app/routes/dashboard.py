"""Dashboard route — per-source extraction health overview."""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authz import Principal, get_principal
from app.core.database import get_db
from app.models.article import Article
from app.models.extraction_run import ExtractionRun, RunStatus
from app.models.image import ArticleImage
from app.models.job import Job
from app.models.product import Product
from app.models.source import DocumentationSource, SourceStatus
from app.models.vendor import Vendor
from app.schemas.dashboard import (
    DashboardEnrichmentResponse, DashboardOverviewResponse, DashboardResponse,
    DashboardSourceRow, DashboardSummary, EnrichmentAggregate, OverviewAggregate,
    OverviewEnrichment, OverviewEscalation, OverviewLastRun, OverviewSourceRow,
    SourceEnrichmentRow,
)

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/sources", response_model=DashboardResponse)
async def dashboard_sources(
    stale_days: int = Query(30, ge=1),
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    now = datetime.now(timezone.utc)

    visible = principal.visible_vendor_ids()
    if visible is not None and not visible:
        return DashboardResponse(
            summary=DashboardSummary(total=0, never_extracted=0, stale=0, failing=0, running=0),
            sources=[],
        )

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
    if visible is not None:
        rows_q = rows_q.where(Product.vendor_id.in_(visible))
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


@router.get("/enrichment", response_model=DashboardEnrichmentResponse)
async def dashboard_enrichment(
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """Per-source and corpus-wide image-enrichment progress."""
    visible = principal.visible_vendor_ids()
    if visible is not None and not visible:
        return DashboardEnrichmentResponse(
            aggregate=EnrichmentAggregate(described=0, pending=0, sources_with_backlog=0), sources=[]
        )

    described_c = func.count().filter(ArticleImage.description.isnot(None))
    pending_c = func.count().filter(
        and_(ArticleImage.description.is_(None), ArticleImage.is_meaningful.isnot(False))
    )
    q = (
        select(
            DocumentationSource.id, DocumentationSource.name,
            Vendor.name.label("vendor"), Product.name.label("product"),
            described_c.label("described"), pending_c.label("pending"),
        )
        .select_from(ArticleImage)
        .join(Article, Article.id == ArticleImage.article_id)
        .join(DocumentationSource, DocumentationSource.id == Article.source_id)
        .join(Product, Product.id == DocumentationSource.product_id)
        .join(Vendor, Vendor.id == Product.vendor_id)
        .group_by(DocumentationSource.id, DocumentationSource.name, Vendor.name, Product.name)
    )
    if visible is not None:
        q = q.where(Product.vendor_id.in_(visible))
    rows = (await db.execute(q)).all()

    active = set((await db.execute(
        select(ExtractionRun.source_id).where(
            ExtractionRun.status.in_([RunStatus.PENDING, RunStatus.RUNNING, RunStatus.PAUSED])
        )
    )).scalars().all())

    out = [
        SourceEnrichmentRow(
            source_id=r.id, vendor=r.vendor, product=r.product, name=r.name,
            described=r.described, pending=r.pending, active_run=r.id in active,
        )
        for r in rows
    ]
    return DashboardEnrichmentResponse(
        aggregate=EnrichmentAggregate(
            described=sum(r.described for r in out),
            pending=sum(r.pending for r in out),
            sources_with_backlog=sum(1 for r in out if r.pending > 0),
        ),
        sources=out,
    )


@router.get("/overview", response_model=DashboardOverviewResponse)
async def dashboard_overview(
    stale_days: int = Query(30, ge=1),
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    now = datetime.now(timezone.utc)
    visible = principal.visible_vendor_ids()
    if visible is not None and not visible:
        return DashboardOverviewResponse(
            aggregate=OverviewAggregate(
                total=0, never_extracted=0, stale=0, failing=0, running=0,
                enrichment=EnrichmentAggregate(described=0, pending=0, sources_with_backlog=0),
                escalation_sources_with_warning=0,
            ),
            sources=[],
        )

    # Article counts (active only).
    counts: dict = {}
    for sid, n in await db.execute(
        select(Article.source_id, func.count())
        .where(Article.removed_at.is_(None)).group_by(Article.source_id)
    ):
        counts[sid] = n

    # Latest run per source (same DISTINCT ON as /sources; PENDING de-prioritised).
    _pending_last = case((ExtractionRun.status == RunStatus.PENDING, 1), else_=0)
    latest_run: dict = {}
    for run in (await db.execute(
        select(ExtractionRun).distinct(ExtractionRun.source_id)
        .order_by(ExtractionRun.source_id, _pending_last, ExtractionRun.started_at.desc())
    )).scalars():
        latest_run[run.source_id] = run

    # Enrichment counts per source (same predicate as /enrichment).
    enr: dict = {}
    described_c = func.count().filter(ArticleImage.description.isnot(None))
    pending_c = func.count().filter(
        and_(ArticleImage.description.is_(None), ArticleImage.is_meaningful.isnot(False))
    )
    for sid, d, p in await db.execute(
        select(Article.source_id, described_c, pending_c)
        .select_from(ArticleImage).join(Article, Article.id == ArticleImage.article_id)
        .group_by(Article.source_id)
    ):
        enr[sid] = (d, p)

    active = set((await db.execute(
        select(ExtractionRun.source_id).where(
            ExtractionRun.status.in_([RunStatus.PENDING, RunStatus.RUNNING, RunStatus.PAUSED])
        )
    )).scalars().all())

    rows_q = (
        select(
            DocumentationSource, Vendor.name.label("vendor"), Product.name.label("product"),
            Job.id.label("job_id"), Job.name.label("job_name"), Job.next_run_at.label("next_run_at"),
        )
        .join(Product, DocumentationSource.product_id == Product.id)
        .join(Vendor, Product.vendor_id == Vendor.id)
        .outerjoin(Job, DocumentationSource.job_id == Job.id)
        .order_by(Vendor.name, Product.name, DocumentationSource.name)
    )
    if visible is not None:
        rows_q = rows_q.where(Product.vendor_id.in_(visible))
    rows = (await db.execute(rows_q)).all()

    out: list[OverviewSourceRow] = []
    total = never = stale = failing = running = esc_warn = 0
    agg_described = agg_pending = backlog = 0
    stale_cutoff = now - timedelta(days=stale_days)

    for src, vendor, product, job_id, job_name, next_run_at in rows:
        total += 1
        last = src.last_extracted_at
        age = int((now - last).total_seconds()) if last else None
        if last is None:
            never += 1
        elif last < stale_cutoff:
            stale += 1
        if src.status == SourceStatus.FAILED:
            failing += 1
        # Count the same active set the "Running" tile-filter uses (row.active_run),
        # so the tile count always matches the number of rows the filter shows.
        if src.id in active:
            running += 1

        run = latest_run.get(src.id)
        last_run = OverviewLastRun(
            run_id=run.id, status=run.status.value, new=run.articles_extracted,
            updated=run.articles_updated, unchanged=run.articles_unchanged,
        ) if run else None

        d, p = enr.get(src.id, (0, 0))
        agg_described += d
        agg_pending += p
        if p > 0:
            backlog += 1

        esc_list = (run.escalation_pending if run else None) or []
        escalation = OverviewEscalation(
            warning=bool(esc_list), pending_count=len(esc_list),
            run_id=run.id if esc_list else None,
        )
        if esc_list:
            esc_warn += 1

        out.append(OverviewSourceRow(
            id=src.id, name=src.name, vendor=vendor, product=product,
            source_type=src.source_type, status=src.status.value,
            last_extracted_at=last.isoformat() if last else None, age_seconds=age,
            article_count=counts.get(src.id, 0), last_run=last_run,
            enrichment=OverviewEnrichment(described=d, pending=p),
            escalation=escalation, active_run=src.id in active,
            job_id=job_id, job_name=job_name,
            next_run_at=next_run_at.isoformat() if next_run_at else None,
        ))

    return DashboardOverviewResponse(
        aggregate=OverviewAggregate(
            total=total, never_extracted=never, stale=stale, failing=failing, running=running,
            enrichment=EnrichmentAggregate(
                described=agg_described, pending=agg_pending, sources_with_backlog=backlog),
            escalation_sources_with_warning=esc_warn,
        ),
        sources=out,
    )
