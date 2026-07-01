"""Article query routes."""

import base64
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func, and_, or_, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.models.article import Article
from app.models.article_version import ArticleVersion
from app.models.extraction_run import ExtractionRun, RunStatus
from app.models.product import Product
from app.models.source import DocumentationSource
from app.models.toc import TOCEntry
from app.schemas.article import (
    ArticleResponse,
    ArticleDetailResponse,
    ArticleImageResponse,
    ArticleListResponse,
    NamedRef,
    ChapterRef,
    TOCEntryResponse,
    TOCResponse,
)
from app.schemas.search import (
    ArticleSearchResponse,
    ArticleSearchResultItem,
    ChangeStatus,
    FacetCount,
    Facets,
    encode_cursor,
    decode_cursor,
)
from app.schemas.version import (
    ArticleVersionResponse,
    ArticleVersionDetailResponse,
    ArticleVersionListResponse,
    VersionDiffResponse,
)
from app.services.diffing import compute_unified_diff

router = APIRouter(prefix="/api/articles", tags=["articles"])


async def _get_article_or_404(db: AsyncSession, article_id: uuid.UUID) -> Article:
    result = await db.execute(select(Article).where(Article.id == article_id))
    article = result.scalar_one_or_none()
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")
    return article


@router.get("", response_model=ArticleSearchResponse)
async def list_articles(
    source_id: uuid.UUID | None = Query(None),
    toc_entry_id: uuid.UUID | None = Query(None),
    search: str | None = Query(None),
    # ── New enhanced filtering params (all optional, backward compatible) ──
    q: str | None = Query(None, description="Full-text search via PostgreSQL FTS5"),
    date_from: datetime | None = Query(None, alias="from", description="Filter articles extracted on or after this ISO datetime"),
    date_to: datetime | None = Query(None, alias="to", description="Filter articles extracted on or before this ISO datetime"),
    status: str | None = Query(None, description="Filter by change status: new, updated, or unchanged"),
    cursor: str | None = Query(None, description="Base64-encoded cursor for cursor-based pagination"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """List articles with optional filtering, full-text search, and cursor pagination.

    Supports both the legacy offset-based pagination (``skip`` + ``limit``) and
    cursor-based pagination (``cursor`` + ``limit``).  When ``cursor`` is provided
    it takes precedence over ``skip``.

    New parameters ``q``, ``from``, ``to``, ``status`` add FTS5 search, date-range
    filtering, and change-status filtering respectively.  All are optional — when
    omitted, the endpoint behaves identically to the original implementation.

    The response includes ``facets`` with counts per status and per date bucket,
    scoped to the current filter set.
    """
    # ── Validate status param ──
    if status is not None and status not in ("new", "updated", "unchanged"):
        raise HTTPException(
            status_code=422,
            detail="status must be one of: new, updated, unchanged",
        )

    # ── Decode cursor if provided ──
    cursor_sort_value = None
    is_cursor_mode = cursor is not None
    if is_cursor_mode:
        try:
            cursor_key, cursor_val = decode_cursor(cursor)
            cursor_sort_value = int(cursor_val)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    # ── Determine if any new-params are active ──
    using_enhanced = any([q, date_from, date_to, status, is_cursor_mode])

    # ── Build base query ──
    base_query = select(Article)
    count_query = select(func.count(Article.id))

    # Existing filters (backward compat)
    if source_id:
        base_query = base_query.where(Article.source_id == source_id)
        count_query = count_query.where(Article.source_id == source_id)
    if toc_entry_id:
        base_query = base_query.where(Article.toc_entry_id == toc_entry_id)
        count_query = count_query.where(Article.toc_entry_id == toc_entry_id)

    # Legacy ILIKE search on title — still supported for backward compat.
    # When ``q`` is also provided, ``q`` takes precedence (FTS5).
    if search and not q:
        base_query = base_query.where(Article.title.ilike(f"%{search}%"))
        count_query = count_query.where(Article.title.ilike(f"%{search}%"))

    # ── FTS5 full-text search ──
    # search_vector is a GENERATED ALWAYS tsvector column — not a SQLAlchemy
    # mapped column — so we reference it via ``text()`` / ``func.ts_rank`` with
    # ``literal_column`` to keep it outside the ORM's insert/update path.
    fts_rank_col = None
    if q:
        # plainto_tsquery handles user input safely (no special operators).
        ts_query = func.plainto_tsquery("english", q)
        from sqlalchemy import literal_column
        sv = literal_column("articles.search_vector")
        base_query = base_query.where(sv.op("@@")(ts_query))
        count_query = count_query.where(sv.op("@@")(ts_query))
        # ts_rank for relevance ordering
        fts_rank_col = func.ts_rank(sv, ts_query).label("search_rank")
        base_query = base_query.add_columns(fts_rank_col)

    # ── Date range filtering (on extracted_at) ──
    if date_from:
        base_query = base_query.where(Article.extracted_at >= date_from)
        count_query = count_query.where(Article.extracted_at >= date_from)
    if date_to:
        base_query = base_query.where(Article.extracted_at <= date_to)
        count_query = count_query.where(Article.extracted_at <= date_to)

    # ── Change-status filtering ──
    # Build on existing tracking: "new" = created_run_id is the latest run,
    # "updated" = has an ArticleVersion in the latest run,
    # "unchanged" = everything else.
    if status:
        # Determine the latest completed run (per source or global).
        latest_run_subq = (
            select(ExtractionRun.id, ExtractionRun.started_at)
            .where(ExtractionRun.status == RunStatus.COMPLETED)
        )
        if source_id:
            latest_run_subq = latest_run_subq.where(
                ExtractionRun.source_id == source_id
            )
        latest_run_subq = latest_run_subq.order_by(
            ExtractionRun.started_at.desc()
        ).limit(1).subquery()

        if status == "new":
            # Articles whose created_run_id matches the latest run.
            base_query = base_query.where(
                Article.created_run_id == select(latest_run_subq.c.id)
            )
            count_query = count_query.where(
                Article.created_run_id == select(latest_run_subq.c.id)
            )
        elif status == "updated":
            # Articles that have an ArticleVersion in the latest run.
            latest_run_id_subq = select(latest_run_subq.c.id)
            updated_article_ids = (
                select(ArticleVersion.article_id)
                .where(ArticleVersion.extraction_run_id == latest_run_id_subq)
                .distinct()
            ).subquery()
            base_query = base_query.where(Article.id.in_(select(updated_article_ids)))
            count_query = count_query.where(Article.id.in_(select(updated_article_ids)))
        elif status == "unchanged":
            # Articles NOT new and NOT updated in the latest run.
            latest_run_id_subq = select(latest_run_subq.c.id)
            new_or_updated_ids = (
                select(Article.id)
                .outerjoin(ArticleVersion, ArticleVersion.article_id == Article.id)
                .where(
                    or_(
                        Article.created_run_id == latest_run_id_subq,
                        ArticleVersion.extraction_run_id == latest_run_id_subq,
                    )
                )
                .distinct()
            ).subquery()
            base_query = base_query.where(~Article.id.in_(select(new_or_updated_ids)))
            count_query = count_query.where(~Article.id.in_(select(new_or_updated_ids)))

    # ── Total count ──
    total = (await db.execute(count_query)).scalar()

    # ── Ordering ──
    if q and fts_rank_col is not None:
        # FTS5 relevance ranking when searching
        base_query = base_query.order_by(text("search_rank DESC"), Article.sort_order)
    else:
        base_query = base_query.order_by(Article.sort_order)

    # ── Pagination ──
    if is_cursor_mode:
        # Cursor-based: filter by sort_order > cursor_sort_value
        base_query = base_query.where(Article.sort_order > cursor_sort_value)
        base_query = base_query.limit(limit + 1)  # fetch one extra to check has_more
    else:
        base_query = base_query.offset(skip).limit(limit)

    # ── Execute query ──
    result = await db.execute(base_query)
    rows = result.all()

    # ── Determine has_more and next_cursor ──
    has_more = False
    next_cursor = None
    if is_cursor_mode and len(rows) > limit:
        has_more = True
        rows = rows[:limit]  # drop the extra row
        last_row = rows[-1]
        # rows are Article objects (with extra columns if FTS)
        last_sort_order = last_row.sort_order if hasattr(last_row, 'sort_order') else last_row[0].sort_order
        next_cursor = encode_cursor("sort_order", str(last_sort_order))

    # ── Extract Article objects from rows (handle both plain and add_columns) ──
    articles = []
    search_ranks = []
    for row in rows:
        if isinstance(row, Article):
            articles.append(row)
            search_ranks.append(None)
        else:
            # Row is a tuple (Article, search_rank) when FTS is active
            articles.append(row[0])
            search_ranks.append(row[1] if len(row) > 1 else None)

    # ── Compute change_status for each article (if status filter or enhanced) ──
    article_statuses = {}
    if using_enhanced or status:
        article_statuses = await _compute_change_statuses(db, [a.id for a in articles], source_id)

    # ── Build result items ──
    result_items = []
    for i, article in enumerate(articles):
        result_items.append(ArticleSearchResultItem(
            id=article.id,
            source_id=article.source_id,
            toc_entry_id=article.toc_entry_id,
            title=article.title,
            source_url=article.source_url,
            last_updated_at=article.last_updated_at,
            sort_order=article.sort_order,
            estimated_tokens=article.estimated_tokens,
            content_size_bytes=article.content_size_bytes,
            created_at=article.created_at,
            extracted_at=article.extracted_at,
            search_rank=search_ranks[i],
            change_status=article_statuses.get(article.id),
        ))

    # ── Compute facets (only in enhanced mode to avoid overhead) ──
    facets = None
    if using_enhanced:
        facets = await _compute_facets(db, source_id, toc_entry_id, q, date_from, date_to, search)

    return ArticleSearchResponse(
        articles=result_items,
        total=total,
        next_cursor=next_cursor,
        has_more=has_more,
        limit=limit,
        facets=facets,
    )


async def _compute_change_statuses(
    db: AsyncSession,
    article_ids: list[uuid.UUID],
    source_id: uuid.UUID | None,
) -> dict[uuid.UUID, str]:
    """Compute change_status for a batch of articles using existing tracking.

    "new": created_run_id matches the latest completed run.
    "updated": has an ArticleVersion in the latest completed run.
    "unchanged": everything else.
    """
    if not article_ids:
        return {}

    # Latest completed run
    latest_run_q = (
        select(ExtractionRun.id, ExtractionRun.started_at)
        .where(ExtractionRun.status == RunStatus.COMPLETED)
    )
    if source_id:
        latest_run_q = latest_run_q.where(ExtractionRun.source_id == source_id)
    latest_run_q = latest_run_q.order_by(ExtractionRun.started_at.desc()).limit(1)
    latest_run = (await db.execute(latest_run_q)).first()

    if not latest_run:
        return {aid: "unchanged" for aid in article_ids}

    latest_run_id = latest_run.id

    # Articles created in the latest run = "new"
    new_ids = set()
    for (aid,) in await db.execute(
        select(Article.id)
        .where(
            Article.id.in_(article_ids),
            Article.created_run_id == latest_run_id,
        )
    ):
        new_ids.add(aid)

    # Articles with versions in the latest run = "updated"
    updated_ids = set()
    for (aid,) in await db.execute(
        select(ArticleVersion.article_id)
        .where(
            ArticleVersion.article_id.in_(article_ids),
            ArticleVersion.extraction_run_id == latest_run_id,
        )
    ):
        updated_ids.add(aid)

    statuses = {}
    for aid in article_ids:
        if aid in new_ids:
            statuses[aid] = "new"
        elif aid in updated_ids:
            statuses[aid] = "updated"
        else:
            statuses[aid] = "unchanged"
    return statuses


async def _compute_facets(
    db: AsyncSession,
    source_id: uuid.UUID | None,
    toc_entry_id: uuid.UUID | None,
    q: str | None,
    date_from: datetime | None,
    date_to: datetime | None,
    search: str | None,
) -> Facets:
    """Compute facet counts per status and per date bucket, scoped to the
    current filter set (excluding the dimension being faceted)."""
    # Status facets: counts of new/updated/unchanged
    status_facets = []

    # Build the base filter (all filters EXCEPT status, since status facets
    # should show what the counts would be if the user removed the status filter)
    def _base_filter(query):
        if source_id:
            query = query.where(Article.source_id == source_id)
        if toc_entry_id:
            query = query.where(Article.toc_entry_id == toc_entry_id)
        if q:
            from sqlalchemy import literal_column
            sv = literal_column("articles.search_vector")
            ts_q = func.plainto_tsquery("english", q)
            query = query.where(sv.op("@@")(ts_q))
        if date_from:
            query = query.where(Article.extracted_at >= date_from)
        if date_to:
            query = query.where(Article.extracted_at <= date_to)
        if search and not q:
            query = query.where(Article.title.ilike(f"%{search}%"))
        return query

    # Latest run for status classification
    latest_run_q = (
        select(ExtractionRun.id)
        .where(ExtractionRun.status == RunStatus.COMPLETED)
    )
    if source_id:
        latest_run_q = latest_run_q.where(ExtractionRun.source_id == source_id)
    latest_run_q = latest_run_q.order_by(ExtractionRun.started_at.desc()).limit(1)
    latest_run_id = (await db.execute(latest_run_q)).scalar()

    if latest_run_id:
        for st in ("new", "updated", "unchanged"):
            count_q = _base_filter(select(func.count(Article.id)))
            if st == "new":
                count_q = count_q.where(Article.created_run_id == latest_run_id)
            elif st == "updated":
                updated_ids = (
                    select(ArticleVersion.article_id)
                    .where(ArticleVersion.extraction_run_id == latest_run_id)
                    .distinct()
                ).subquery()
                count_q = count_q.where(Article.id.in_(select(updated_ids)))
            else:  # unchanged
                new_or_updated = (
                    select(Article.id)
                    .outerjoin(
                        ArticleVersion, ArticleVersion.article_id == Article.id
                    )
                    .where(
                        or_(
                            Article.created_run_id == latest_run_id,
                            ArticleVersion.extraction_run_id == latest_run_id,
                        )
                    )
                    .distinct()
                ).subquery()
                count_q = count_q.where(~Article.id.in_(select(new_or_updated)))
            count = (await db.execute(count_q)).scalar()
            status_facets.append(FacetCount(label=st, count=count or 0))
    else:
        # No completed runs — all articles are "unchanged"
        total_q = _base_filter(select(func.count(Article.id)))
        total = (await db.execute(total_q)).scalar() or 0
        status_facets = [
            FacetCount(label="new", count=0),
            FacetCount(label="updated", count=0),
            FacetCount(label="unchanged", count=total),
        ]

    # Date bucket facets: counts per month (truncated from extracted_at)
    date_bucket_q = _base_filter(
        select(
            func.to_char(Article.extracted_at, "YYYY-MM").label("bucket"),
            func.count(Article.id).label("cnt"),
        ).group_by(text("bucket")).order_by(text("bucket"))
    )
    date_rows = (await db.execute(date_bucket_q)).all()
    date_facets = [FacetCount(label=r.bucket, count=r.cnt) for r in date_rows]

    return Facets(status=status_facets, date_bucket=date_facets)


@router.get("/{article_id}", response_model=ArticleDetailResponse)
async def get_article(article_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Get a single article with full content, images, and provenance metadata.

    Vendor, product, and parent/top-level chapter are derived (the TOC is the
    source of truth), so they stay correct as the TOC is rebuilt across runs.
    """
    result = await db.execute(
        select(Article)
        .where(Article.id == article_id)
        .options(
            selectinload(Article.images),
            selectinload(Article.source)
            .selectinload(DocumentationSource.product)
            .selectinload(Product.vendor),
        )
    )
    article = result.scalar_one_or_none()
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")

    vendor = product = None
    if article.source is not None and article.source.product is not None:
        prod = article.source.product
        product = NamedRef(id=prod.id, name=prod.name)
        if prod.vendor is not None:
            vendor = NamedRef(id=prod.vendor.id, name=prod.vendor.name)

    # Derive parent (one level up) and top-level (root) chapter from the TOC tree.
    parent_chapter = top_level_chapter = None
    if article.toc_entry_id is not None:
        toc_rows = (
            await db.execute(
                select(TOCEntry.id, TOCEntry.parent_id, TOCEntry.title).where(
                    TOCEntry.source_id == article.source_id
                )
            )
        ).all()
        parent_of = {r.id: r.parent_id for r in toc_rows}
        title_of = {r.id: r.title for r in toc_rows}

        tid = article.toc_entry_id
        pid = parent_of.get(tid)
        if pid is not None and pid in title_of:
            parent_chapter = ChapterRef(id=pid, title=title_of[pid])

        root = tid
        seen: set[uuid.UUID] = set()
        while parent_of.get(root) is not None and root not in seen:
            seen.add(root)
            root = parent_of[root]
        if root in title_of:
            top_level_chapter = ChapterRef(id=root, title=title_of[root])

    return ArticleDetailResponse(
        id=article.id,
        source_id=article.source_id,
        toc_entry_id=article.toc_entry_id,
        title=article.title,
        source_url=article.source_url,
        last_updated_at=article.last_updated_at,
        sort_order=article.sort_order,
        estimated_tokens=article.estimated_tokens,
        content_size_bytes=article.content_size_bytes,
        created_at=article.created_at,
        extracted_at=article.extracted_at,
        content_markdown=article.content_markdown,
        images=[ArticleImageResponse.model_validate(i) for i in article.images],
        vendor=vendor,
        product=product,
        parent_chapter=parent_chapter,
        top_level_chapter=top_level_chapter,
    )


@router.get("/toc/{source_id}", response_model=TOCResponse)
async def get_toc(source_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Get the table of contents for a source, with article IDs."""
    result = await db.execute(
        select(TOCEntry)
        .where(TOCEntry.source_id == source_id)
        .order_by(TOCEntry.sort_order)
    )
    entries = result.scalars().all()

    # Build tree
    entry_map: dict[uuid.UUID, TOCEntryResponse] = {}
    roots: list[TOCEntryResponse] = []

    # First pass: create response objects
    for entry in entries:
        resp = TOCEntryResponse(
            id=entry.id,
            title=entry.title,
            url=entry.url,
            level=entry.level,
            sort_order=entry.sort_order,
            is_article=entry.is_article,
            children=[],
        )
        entry_map[entry.id] = resp

    # Second pass: get article IDs for article entries
    article_result = await db.execute(
        select(Article.id, Article.toc_entry_id).where(
            Article.source_id == source_id,
            Article.toc_entry_id.in_([e.id for e in entries]),
        )
    )
    article_toc_map: dict[uuid.UUID, uuid.UUID] = {}
    for row in article_result:
        article_toc_map[row.toc_entry_id] = row.id

    for entry in entries:
        resp = entry_map[entry.id]
        if entry.id in article_toc_map:
            resp.article_id = article_toc_map[entry.id]

    # Third pass: build hierarchy
    for entry in entries:
        resp = entry_map[entry.id]
        if entry.parent_id and entry.parent_id in entry_map:
            entry_map[entry.parent_id].children.append(resp)
        else:
            roots.append(resp)

    return TOCResponse(source_id=source_id, entries=roots)


@router.get(
    "/{article_id}/versions", response_model=ArticleVersionListResponse
)
async def list_article_versions(
    article_id: uuid.UUID,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """List an article's historical snapshots, newest first.

    Each ArticleVersion holds a *previous* content snapshot; the live content
    is on the Article itself (exposed here as ``current_hash``).
    """
    article = await _get_article_or_404(db, article_id)

    count_query = select(func.count(ArticleVersion.id)).where(
        ArticleVersion.article_id == article_id
    )
    total = (await db.execute(count_query)).scalar()

    # Select metadata columns only — version bodies can be large.
    rows = await db.execute(
        select(
            ArticleVersion.id,
            ArticleVersion.article_id,
            ArticleVersion.extraction_run_id,
            ArticleVersion.content_hash,
            ArticleVersion.diff_text.isnot(None).label("has_diff"),
            func.coalesce(
                func.octet_length(ArticleVersion.content_markdown), 0
            ).label("content_size_bytes"),
            ArticleVersion.extracted_at,
            ArticleVersion.source_url,
            ExtractionRun.version.label("run_version"),
        )
        .outerjoin(ExtractionRun, ExtractionRun.id == ArticleVersion.extraction_run_id)
        .where(ArticleVersion.article_id == article_id)
        .order_by(ArticleVersion.extracted_at.desc())
        .offset(skip)
        .limit(limit)
    )

    versions = [
        ArticleVersionResponse(
            id=r.id,
            article_id=r.article_id,
            extraction_run_id=r.extraction_run_id,
            content_hash=r.content_hash,
            has_diff=r.has_diff,
            content_size_bytes=r.content_size_bytes,
            extracted_at=r.extracted_at,
            version=r.run_version,
            source_url=r.source_url,
        )
        for r in rows
    ]

    return ArticleVersionListResponse(
        article_id=article_id,
        current_hash=article.content_hash,
        versions=versions,
        total=total,
    )


async def _get_version_or_404(
    db: AsyncSession, article_id: uuid.UUID, version_id: uuid.UUID
) -> ArticleVersion:
    result = await db.execute(
        select(ArticleVersion).where(
            ArticleVersion.id == version_id,
            ArticleVersion.article_id == article_id,
        )
    )
    version = result.scalar_one_or_none()
    if not version:
        raise HTTPException(status_code=404, detail="Version not found")
    return version


@router.get(
    "/{article_id}/versions/{version_id}",
    response_model=ArticleVersionDetailResponse,
)
async def get_article_version(
    article_id: uuid.UUID,
    version_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Fetch a single version with its full content body (for side-by-side view)."""
    version = await _get_version_or_404(db, article_id, version_id)
    return ArticleVersionDetailResponse(
        id=version.id,
        article_id=version.article_id,
        extraction_run_id=version.extraction_run_id,
        content_hash=version.content_hash,
        has_diff=version.diff_text is not None,
        content_size_bytes=len(version.content_markdown.encode("utf-8")),
        extracted_at=version.extracted_at,
        source_url=version.source_url,
        content_markdown=version.content_markdown,
    )


@router.get(
    "/{article_id}/versions/{version_id}/diff",
    response_model=VersionDiffResponse,
)
async def get_version_diff(
    article_id: uuid.UUID,
    version_id: uuid.UUID,
    against: str = Query(
        "next",
        pattern="^(next|current)$",
        description="Diff this version against the content that replaced it "
        "('next') or the live article ('current').",
    ),
    db: AsyncSession = Depends(get_db),
):
    """Return the diff from a version's content to a newer state.

    A version stores the content that was *superseded*; its ``diff_text`` (when
    present) describes the transition to the content that replaced it. With
    ``against=next`` we return that stored diff when available, otherwise we
    compute one. ``against=current`` always diffs against the live article.
    """
    version = await _get_version_or_404(db, article_id, version_id)
    article = await _get_article_or_404(db, article_id)

    # Resolve the "newer" side of the diff.
    if against == "current":
        new_content = article.content_markdown
        to_label = "current"
    else:
        # The content that replaced this version = the next-newer version's
        # content, or the live article if this is the most recent version.
        newer = await db.execute(
            select(ArticleVersion)
            .where(
                ArticleVersion.article_id == article_id,
                ArticleVersion.extracted_at > version.extracted_at,
            )
            .order_by(ArticleVersion.extracted_at.asc())
            .limit(1)
        )
        newer_version = newer.scalar_one_or_none()
        if newer_version is not None:
            new_content = newer_version.content_markdown
            to_label = f"version:{newer_version.id}"
        else:
            new_content = article.content_markdown
            to_label = "current"

        # Prefer the diff Firecrawl already computed for this transition.
        if version.diff_text:
            return VersionDiffResponse(
                article_id=article_id,
                version_id=version_id,
                from_label=f"version:{version_id}",
                to_label=to_label,
                diff_text=version.diff_text,
                computed=False,
            )

    diff_text = compute_unified_diff(
        version.content_markdown,
        new_content,
        from_label=f"version:{version_id}",
        to_label=to_label,
    )
    return VersionDiffResponse(
        article_id=article_id,
        version_id=version_id,
        from_label=f"version:{version_id}",
        to_label=to_label,
        diff_text=diff_text,
        computed=True,
    )
