"""DocumentationSource CRUD routes."""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.models.article import Article
from app.models.article_version import ArticleVersion
from app.models.extraction_run import ExtractionRun, RunStatus
from app.models.schedule import Schedule
from app.models.source import DocumentationSource
from app.models.toc import TOCEntry
from app.schemas.browse import BrowseTOCEntry, RemovedArticle, BrowseResponse
from app.schemas.schedule import ScheduleConfig, ScheduleResponse, ScheduleLastRun
from app.schemas.source import (
    SourceCreate,
    SourceUpdate,
    SourceResponse,
    SourceListResponse,
)
from app.schemas.version import ChangelogEntry, ChangelogResponse
from app.services.cron import build_cron, compute_next_run

router = APIRouter(prefix="/api/sources", tags=["sources"])


@router.post("", response_model=SourceResponse, status_code=201)
async def create_source(body: SourceCreate, db: AsyncSession = Depends(get_db)):
    """Add a new documentation source to extract."""
    source = DocumentationSource(
        vendor_id=body.vendor_id,
        name=body.name,
        base_url=body.base_url,
    )
    db.add(source)
    await db.commit()
    await db.refresh(source)
    return source


@router.get("", response_model=SourceListResponse)
async def list_sources(
    vendor_id: uuid.UUID | None = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """List documentation sources, optionally filtered by vendor."""
    base_query = select(DocumentationSource)
    count_query = select(func.count(DocumentationSource.id))

    if vendor_id:
        base_query = base_query.where(DocumentationSource.vendor_id == vendor_id)
        count_query = count_query.where(DocumentationSource.vendor_id == vendor_id)

    total_result = await db.execute(count_query)
    total = total_result.scalar()

    result = await db.execute(
        base_query.order_by(DocumentationSource.name).offset(skip).limit(limit)
    )
    sources = result.scalars().all()

    return SourceListResponse(sources=sources, total=total)


@router.get("/{source_id}", response_model=SourceResponse)
async def get_source(source_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Get a documentation source by ID."""
    result = await db.execute(
        select(DocumentationSource).where(DocumentationSource.id == source_id)
    )
    source = result.scalar_one_or_none()
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")
    return source


@router.patch("/{source_id}", response_model=SourceResponse)
async def update_source(
    source_id: uuid.UUID, body: SourceUpdate, db: AsyncSession = Depends(get_db)
):
    """Update a documentation source."""
    result = await db.execute(
        select(DocumentationSource).where(DocumentationSource.id == source_id)
    )
    source = result.scalar_one_or_none()
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")

    if body.name is not None:
        source.name = body.name
    if body.base_url is not None:
        source.base_url = body.base_url

    await db.commit()
    await db.refresh(source)
    return source


@router.delete("/{source_id}", status_code=204)
async def delete_source(source_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Delete a documentation source and all associated data."""
    result = await db.execute(
        select(DocumentationSource).where(DocumentationSource.id == source_id)
    )
    source = result.scalar_one_or_none()
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")

    await db.delete(source)
    await db.commit()


@router.get("/{source_id}/changelog", response_model=ChangelogResponse)
async def get_source_changelog(
    source_id: uuid.UUID,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Consolidated changelog: every article change for a source, newest first.

    One entry per ArticleVersion (a superseded snapshot), joined to its article
    for the title. Link each entry to the article version-diff endpoint for the
    detailed change.
    """
    result = await db.execute(
        select(DocumentationSource.id).where(DocumentationSource.id == source_id)
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Source not found")

    count_query = (
        select(func.count(ArticleVersion.id))
        .join(Article, Article.id == ArticleVersion.article_id)
        .where(Article.source_id == source_id)
    )
    total = (await db.execute(count_query)).scalar()

    rows = await db.execute(
        select(
            ArticleVersion.id.label("version_id"),
            ArticleVersion.article_id,
            Article.title,
            ArticleVersion.extraction_run_id,
            ArticleVersion.extracted_at,
            ArticleVersion.diff_text.isnot(None).label("has_diff"),
        )
        .join(Article, Article.id == ArticleVersion.article_id)
        .where(Article.source_id == source_id)
        .order_by(ArticleVersion.extracted_at.desc())
        .offset(skip)
        .limit(limit)
    )

    entries = [
        ChangelogEntry(
            article_id=r.article_id,
            title=r.title,
            version_id=r.version_id,
            extraction_run_id=r.extraction_run_id,
            extracted_at=r.extracted_at,
            has_diff=r.has_diff,
        )
        for r in rows
    ]

    return ChangelogResponse(source_id=source_id, entries=entries, total=total)


@router.get("/{source_id}/browse", response_model=BrowseResponse)
async def browse_source(source_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Return the TOC tree annotated for the documentation browser.

    Each article node carries a ``change_status`` relative to the most recent
    completed run (``new`` if first seen in that run, ``updated`` if a version
    snapshot was created by it, else ``unchanged``), a version count, and the
    last-updated timestamp. Articles no longer present in the rebuilt TOC
    (``toc_entry_id IS NULL``) are returned separately as ``removed``.
    """
    src = await db.execute(
        select(DocumentationSource.id).where(DocumentationSource.id == source_id)
    )
    if src.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Source not found")

    # Most recent completed run — the baseline for change annotations.
    latest_run = (
        await db.execute(
            select(ExtractionRun)
            .where(
                ExtractionRun.source_id == source_id,
                ExtractionRun.status == RunStatus.COMPLETED,
            )
            .order_by(ExtractionRun.started_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    latest_run_id = latest_run.id if latest_run else None
    latest_started = latest_run.started_at if latest_run else None

    # Version counts per article, and which articles changed in the latest run.
    version_counts: dict[uuid.UUID, int] = {}
    for article_id, count in await db.execute(
        select(ArticleVersion.article_id, func.count())
        .join(Article, Article.id == ArticleVersion.article_id)
        .where(Article.source_id == source_id)
        .group_by(ArticleVersion.article_id)
    ):
        version_counts[article_id] = count

    updated_in_latest: set[uuid.UUID] = set()
    if latest_run_id is not None:
        for (article_id,) in await db.execute(
            select(ArticleVersion.article_id)
            .where(ArticleVersion.extraction_run_id == latest_run_id)
            .distinct()
        ):
            updated_in_latest.add(article_id)

    # All articles for the source (lightweight columns).
    articles = (
        await db.execute(
            select(
                Article.id,
                Article.toc_entry_id,
                Article.title,
                Article.source_url,
                Article.created_at,
                Article.last_updated_at,
                Article.extracted_at,
            ).where(Article.source_id == source_id)
        )
    ).all()

    def classify(article) -> str:
        if latest_started is not None and article.created_at >= latest_started:
            return "new"
        if article.id in updated_in_latest:
            return "updated"
        return "unchanged"

    article_by_toc: dict[uuid.UUID, object] = {}
    removed: list[RemovedArticle] = []
    for a in articles:
        if a.toc_entry_id is None:
            removed.append(RemovedArticle(
                article_id=a.id,
                title=a.title,
                source_url=a.source_url,
                last_extracted_at=a.extracted_at,
                version_count=version_counts.get(a.id, 0),
            ))
        else:
            article_by_toc[a.toc_entry_id] = a

    # Build the annotated TOC tree (same shape as the existing TOC endpoint).
    toc_rows = (
        await db.execute(
            select(TOCEntry)
            .where(TOCEntry.source_id == source_id)
            .order_by(TOCEntry.sort_order)
        )
    ).scalars().all()

    node_map: dict[uuid.UUID, BrowseTOCEntry] = {}
    for entry in toc_rows:
        article = article_by_toc.get(entry.id)
        node_map[entry.id] = BrowseTOCEntry(
            id=entry.id,
            title=entry.title,
            url=entry.url,
            level=entry.level,
            sort_order=entry.sort_order,
            is_article=entry.is_article,
            article_id=article.id if article else None,
            change_status=classify(article) if article else None,
            version_count=version_counts.get(article.id, 0) if article else 0,
            last_updated_at=article.last_updated_at if article else None,
            children=[],
        )

    roots: list[BrowseTOCEntry] = []
    for entry in toc_rows:
        node = node_map[entry.id]
        if entry.parent_id and entry.parent_id in node_map:
            node_map[entry.parent_id].children.append(node)
        else:
            roots.append(node)

    return BrowseResponse(
        source_id=source_id,
        latest_run_id=latest_run_id,
        entries=roots,
        removed=removed,
    )


async def _schedule_response(db: AsyncSession, sched: Schedule) -> ScheduleResponse:
    last_run = None
    if sched.last_run_id is not None:
        run = (
            await db.execute(
                select(ExtractionRun).where(ExtractionRun.id == sched.last_run_id)
            )
        ).scalar_one_or_none()
        if run is not None:
            last_run = ScheduleLastRun(
                id=run.id, status=run.status.value, completed_at=run.completed_at
            )
    return ScheduleResponse(
        source_id=sched.source_id,
        enabled=sched.enabled,
        frequency=sched.frequency,
        time_of_day=sched.time_of_day,
        day_of_week=sched.day_of_week,
        day_of_month=sched.day_of_month,
        cron=sched.cron,
        timezone=sched.timezone,
        next_run_at=sched.next_run_at,
        last_run_at=sched.last_run_at,
        last_run=last_run,
    )


@router.get("/{source_id}/schedule", response_model=ScheduleResponse)
async def get_schedule(source_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    sched = (
        await db.execute(select(Schedule).where(Schedule.source_id == source_id))
    ).scalar_one_or_none()
    if sched is None:
        raise HTTPException(status_code=404, detail="No schedule for this source")
    return await _schedule_response(db, sched)


@router.put("/{source_id}/schedule", response_model=ScheduleResponse)
async def put_schedule(
    source_id: uuid.UUID,
    body: ScheduleConfig,
    db: AsyncSession = Depends(get_db),
):
    source = (
        await db.execute(
            select(DocumentationSource).where(DocumentationSource.id == source_id)
        )
    ).scalar_one_or_none()
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")

    cron = build_cron(
        body.frequency, body.time_of_day, body.day_of_week, body.day_of_month
    )
    now = datetime.now(timezone.utc)
    next_run_at = compute_next_run(cron, body.timezone, now) if body.enabled else None

    sched = (
        await db.execute(select(Schedule).where(Schedule.source_id == source_id))
    ).scalar_one_or_none()
    if sched is None:
        sched = Schedule(source_id=source_id)
        db.add(sched)
    sched.enabled = body.enabled
    sched.frequency = body.frequency
    sched.time_of_day = body.time_of_day
    sched.day_of_week = body.day_of_week
    sched.day_of_month = body.day_of_month
    sched.cron = cron
    sched.timezone = body.timezone
    sched.next_run_at = next_run_at
    await db.commit()
    await db.refresh(sched)
    return await _schedule_response(db, sched)


@router.delete("/{source_id}/schedule", status_code=204)
async def delete_schedule(source_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    sched = (
        await db.execute(select(Schedule).where(Schedule.source_id == source_id))
    ).scalar_one_or_none()
    if sched is not None:
        await db.delete(sched)
        await db.commit()
    return None
