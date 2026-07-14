"""Article query routes."""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select, func, and_, or_, text, exists, false, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.authz import Principal, get_principal, authorize_source, authorize_article
from app.core.database import get_db
from app.models.article import Article
from app.models.article_version import ArticleVersion
from app.models.extraction_run import ExtractionRun, RunStatus
from app.models.product import Product
from app.models.source import DocumentationSource
from app.models.toc import TOCEntry
from app.schemas.article import (
    ArticleDetailResponse,
    ArticleImageResponse,
    NamedRef,
    ChapterRef,
    TOCEntryResponse,
    TOCResponse,
)
from app.schemas.delta import decode_delta_cursor
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
from app.services import delta_feed
from app.services.diffing import compute_unified_diff
# Reuse the exporter's canonical FTS expression so the planner uses the existing
# GIN expression index (ix_articles_fts) — no second index/column is introduced.
from app.services.exporter import _TSV

router = APIRouter(prefix="/api/articles", tags=["articles"])


async def _get_article_or_404(db: AsyncSession, article_id: uuid.UUID) -> Article:
    result = await db.execute(select(Article).where(Article.id == article_id))
    article = result.scalar_one_or_none()
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")
    return article


# ── FTS helpers — reuse the exporter's _TSV expression so the planner uses the
# existing ix_articles_fts GIN index (no second column/index). ─────────────

def _fts_predicate(q: str):
    """`<tsv> @@ plainto_tsquery(...)` membership test for the search term."""
    return text(f"{_TSV} @@ plainto_tsquery('english', :fts_q)").bindparams(fts_q=q)


def _fts_rank(q: str):
    """`ts_rank(<tsv>, plainto_tsquery(...))` relevance score — a function
    expression (not raw text) so it can be ``.label()``-ed and ordered by."""
    return func.ts_rank(text(_TSV), func.plainto_tsquery("english", q))


def _filter_conditions(
    source_id, toc_entry_id, q, search, date_from, date_to, visible_vendor_ids=None
):
    """Common WHERE conditions shared by the main query, the count, and facets.

    ``q`` (FTS) takes precedence over the legacy ``search`` ILIKE. Excludes the
    change-status dimension (applied separately) so facets can reuse this.

    ``visible_vendor_ids``, when not None, restricts to articles whose source's
    product's vendor is in that set (row-level authz filter for list endpoints
    with no explicit ``source_id``).
    """
    conds = []
    if source_id:
        conds.append(Article.source_id == source_id)
    if toc_entry_id:
        conds.append(Article.toc_entry_id == toc_entry_id)
    if q:
        conds.append(_fts_predicate(q))
    elif search:
        conds.append(Article.title.ilike(f"%{search}%"))
    if date_from:
        conds.append(Article.extracted_at >= date_from)
    if date_to:
        conds.append(Article.extracted_at <= date_to)
    if visible_vendor_ids is not None:
        visible_sources = (
            select(DocumentationSource.id)
            .join(Product, DocumentationSource.product_id == Product.id)
            .where(Product.vendor_id.in_(visible_vendor_ids))
            .scalar_subquery()
        )
        conds.append(Article.source_id.in_(visible_sources))
    return conds


def _is_updated_exists(latest_run_id):
    """Correlated EXISTS: this article has an ArticleVersion in the latest run."""
    return exists().where(
        and_(
            ArticleVersion.article_id == Article.id,
            ArticleVersion.extraction_run_id == latest_run_id,
        )
    )


def _status_condition(status: ChangeStatus, latest_run_id):
    """SQL predicate for a change-status filter, consistent with the per-item
    classification (new > updated > unchanged)."""
    if latest_run_id is None:
        # No completed run yet: nothing is new/updated; everything is unchanged.
        return false() if status in (ChangeStatus.NEW, ChangeStatus.UPDATED) else true()
    is_new = Article.created_run_id == latest_run_id
    # NULL-safe negation: a NULL created_run_id is "not new" (== yields NULL).
    not_new = Article.created_run_id.is_distinct_from(latest_run_id)
    is_updated = _is_updated_exists(latest_run_id)
    if status == ChangeStatus.NEW:
        return is_new
    if status == ChangeStatus.UPDATED:
        return and_(is_updated, not_new)
    return and_(not_new, ~is_updated)


async def _latest_run_id(db: AsyncSession, source_id: uuid.UUID | None):
    """The id of the most recent completed run (scoped to source if given)."""
    stmt = select(ExtractionRun.id).where(ExtractionRun.status == RunStatus.COMPLETED)
    if source_id:
        stmt = stmt.where(ExtractionRun.source_id == source_id)
    stmt = stmt.order_by(ExtractionRun.started_at.desc()).limit(1)
    return (await db.execute(stmt)).scalar()


@router.get("", response_model=ArticleSearchResponse)
async def list_articles(
    source_id: uuid.UUID | None = Query(None),
    toc_entry_id: uuid.UUID | None = Query(None),
    search: str | None = Query(None, description="Legacy ILIKE title match (superseded by q)"),
    # ── Enhanced filtering params (all optional, backward compatible) ──
    q: str | None = Query(None, description="Full-text search over title + content"),
    date_from: datetime | None = Query(None, alias="from", description="Articles extracted on/after this ISO datetime"),
    date_to: datetime | None = Query(None, alias="to", description="Articles extracted on/before this ISO datetime"),
    status: ChangeStatus | None = Query(None, description="Change status: new, updated, or unchanged"),
    cursor: str | None = Query(None, description="Opaque cursor from a prior response's next_cursor"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """List articles with optional full-text search, date-range and change-status
    filtering, facets, and forward pagination.

    Pagination: every response carries ``next_cursor`` whenever ``has_more`` is
    true — including the default first page — so a client pages forward by
    echoing it back as ``?cursor=`` (resending the same filters, notably ``q``).
    ``skip`` remains supported for legacy offset paging. With no enhanced params
    the endpoint is a drop-in superset of the original list response.
    """
    if source_id:
        await authorize_source(db, principal, source_id, write=False)

    visible_vendor_ids = None
    if not source_id:
        visible = principal.visible_vendor_ids()
        if visible is not None:
            if not visible:
                return ArticleSearchResponse(
                    articles=[], total=0, next_cursor=None, has_more=False,
                    limit=limit, facets=None,
                )
            visible_vendor_ids = visible

    searching = bool(q)

    # ── Decode cursor (opaque; shape depends on browse vs. search ordering) ──
    cursor_payload: dict | None = None
    if cursor is not None:
        try:
            cursor_payload = decode_cursor(cursor)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    using_enhanced = any([q, date_from, date_to, status is not None, cursor is not None])
    first_page = cursor is None

    conds = _filter_conditions(
        source_id, toc_entry_id, q, search, date_from, date_to, visible_vendor_ids
    )

    # Latest completed run — computed once and reused for the status filter,
    # per-item change_status, and facets. Only needed in enhanced mode.
    latest_run_id = await _latest_run_id(db, source_id) if using_enhanced else None
    want_status = using_enhanced          # emit change_status per item in enhanced mode
    status_cols = want_status and latest_run_id is not None

    if status is not None:
        conds.append(_status_condition(status, latest_run_id))

    # ── Build the page query ──
    query = select(Article)
    if conds:
        query = query.where(*conds)
    rank_col = None
    if searching:
        rank_col = _fts_rank(q).label("search_rank")
        query = query.add_columns(rank_col)
    if status_cols:
        query = query.add_columns(
            (Article.created_run_id == latest_run_id).label("is_new"),
            _is_updated_exists(latest_run_id).label("is_updated"),
        )

    # ── Ordering + pagination (fetch limit+1 to detect has_more) ──
    if searching:
        # Relevance order; offset-paged (rank is deterministic for a fixed q).
        query = query.order_by(rank_col.desc(), Article.sort_order, Article.id)
        if cursor_payload is not None:
            if "off" not in cursor_payload:
                raise HTTPException(status_code=422, detail="Cursor does not match this query")
            offset = int(cursor_payload["off"])
        else:
            offset = skip
        query = query.offset(offset).limit(limit + 1)
    else:
        # Stable keyset order on (sort_order, id).
        query = query.order_by(Article.sort_order, Article.id)
        if cursor_payload is not None:
            try:
                c_order = int(cursor_payload["o"])
                c_id = uuid.UUID(str(cursor_payload["id"]))
            except (KeyError, ValueError):
                raise HTTPException(status_code=422, detail="Cursor does not match this query")
            query = query.where(
                or_(
                    Article.sort_order > c_order,
                    and_(Article.sort_order == c_order, Article.id > c_id),
                )
            )
            query = query.limit(limit + 1)
        else:
            query = query.offset(skip).limit(limit + 1)

    rows = (await db.execute(query)).all()
    has_more = len(rows) > limit
    rows = rows[:limit]

    # ── Map rows → result items ──
    result_items: list[ArticleSearchResultItem] = []
    for row in rows:
        article = row[0]
        rank = row._mapping.get("search_rank") if searching else None
        if status_cols:
            if row._mapping.get("is_new"):
                cs = ChangeStatus.NEW
            elif row._mapping.get("is_updated"):
                cs = ChangeStatus.UPDATED
            else:
                cs = ChangeStatus.UNCHANGED
        elif want_status:
            cs = ChangeStatus.UNCHANGED   # enhanced mode, but no completed run yet
        else:
            cs = None
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
            search_rank=rank,
            change_status=cs,
        ))

    # ── next_cursor (works in every mode, first page included) ──
    next_cursor = None
    if has_more and result_items:
        last = result_items[-1]
        if searching:
            next_cursor = encode_cursor({"off": (offset + limit)})
        else:
            next_cursor = encode_cursor({"o": last.sort_order, "id": str(last.id)})

    # ── total (only on the first page; skipped on cursor-continuation) ──
    total = None
    if first_page:
        count_query = select(func.count(Article.id))
        if conds:
            count_query = count_query.where(*conds)
        total = (await db.execute(count_query)).scalar()

    # ── facets (computed once, on the first enhanced page only) ──
    facets = None
    if using_enhanced and first_page:
        facets = await _compute_facets(
            db, source_id=source_id, toc_entry_id=toc_entry_id, q=q,
            search=search, date_from=date_from, date_to=date_to,
            latest_run_id=latest_run_id, visible_vendor_ids=visible_vendor_ids,
        )

    return ArticleSearchResponse(
        articles=result_items,
        total=total,
        next_cursor=next_cursor,
        has_more=has_more,
        limit=limit,
        facets=facets,
    )


async def _compute_facets(
    db: AsyncSession,
    *,
    source_id: uuid.UUID | None,
    toc_entry_id: uuid.UUID | None,
    q: str | None,
    search: str | None,
    date_from: datetime | None,
    date_to: datetime | None,
    latest_run_id,
    visible_vendor_ids=None,
) -> Facets:
    """Facet counts per change-status and per month, scoped to the current
    filter set but excluding the status dimension. Two queries total: one
    conditional-aggregation query for status, one grouped query for dates."""
    conds = _filter_conditions(
        source_id, toc_entry_id, q, search, date_from, date_to, visible_vendor_ids
    )

    # Status facets — a single query using COUNT(*) FILTER (WHERE …), matching
    # the per-item classification (new > updated > unchanged).
    if latest_run_id is not None:
        is_new = Article.created_run_id == latest_run_id
        not_new = Article.created_run_id.is_distinct_from(latest_run_id)
        is_updated = _is_updated_exists(latest_run_id)
        stmt = select(
            func.count().filter(is_new).label("new"),
            func.count().filter(and_(is_updated, not_new)).label("updated"),
            func.count().filter(and_(not_new, ~is_updated)).label("unchanged"),
        ).select_from(Article)
        if conds:
            stmt = stmt.where(*conds)
        r = (await db.execute(stmt)).one()
        status_facets = [
            FacetCount(label="new", count=r.new or 0),
            FacetCount(label="updated", count=r.updated or 0),
            FacetCount(label="unchanged", count=r.unchanged or 0),
        ]
    else:
        stmt = select(func.count()).select_from(Article)
        if conds:
            stmt = stmt.where(*conds)
        total = (await db.execute(stmt)).scalar() or 0
        status_facets = [
            FacetCount(label="new", count=0),
            FacetCount(label="updated", count=0),
            FacetCount(label="unchanged", count=total),
        ]

    # Date-bucket facets — counts per YYYY-MM of extracted_at.
    date_stmt = select(
        func.to_char(Article.extracted_at, "YYYY-MM").label("bucket"),
        func.count().label("cnt"),
    ).select_from(Article)
    if conds:
        date_stmt = date_stmt.where(*conds)
    date_stmt = date_stmt.group_by(text("bucket")).order_by(text("bucket"))
    date_rows = (await db.execute(date_stmt)).all()
    date_facets = [FacetCount(label=row.bucket, count=row.cnt) for row in date_rows]

    return Facets(status=status_facets, date_bucket=date_facets)


@router.get("/delta")
async def article_delta_feed(
    since: str | None = Query(
        None,
        description="Opaque cursor from a prior pull's control record. "
        "Omit for a full bootstrap snapshot.",
    ),
    source_id: uuid.UUID | None = Query(None),
    vendor_id: uuid.UUID | None = Query(None),
    bootstrap_after: uuid.UUID | None = Query(
        None, description="Bootstrap only: resume after this article id (ignored when 'since' is set)."
    ),
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """Stream article changes as JSONL for the downstream GraphRAG pipeline.

    - ``since`` omitted → full bootstrap snapshot (every current, visible,
      non-removed article as ``added``); the FIRST line is a control record
      ``{"control":"bootstrap_start","next_since":"..."}`` carrying the
      watermark up front, so a client that keeps it can resume a dropped
      snapshot with ``?bootstrap_after=<highest article id applied>`` and still
      anchor its first incremental pull to the ORIGINAL watermark (a watermark
      recomputed at resume time could miss an update to an already-emitted
      article).
    - ``since`` present → changes after that watermark (added/updated content
      records + removed tombstones), gap-free under concurrent runs.

    The final line is always a control record: ``{"control":"cursor",
    "next_since": "...","count": N}`` — the SAME ``next_since`` as the leading
    ``bootstrap_start`` line for a bootstrap pull. A truncated stream lacks it —
    clients must only advance their stored cursor on a clean finish.
    """
    visible = principal.visible_vendor_ids()

    if since is None:
        gen = delta_feed.stream_bootstrap(
            db, source_id=source_id, vendor_id=vendor_id, visible_vendor_ids=visible,
            bootstrap_after=bootstrap_after,
        )
    else:
        try:
            since_seq = decode_delta_cursor(since)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        gen = delta_feed.stream_delta(
            db, since_seq=since_seq, source_id=source_id, vendor_id=vendor_id,
            visible_vendor_ids=visible,
        )

    return StreamingResponse(gen, media_type="application/x-ndjson")


@router.get("/{article_id}", response_model=ArticleDetailResponse)
async def get_article(
    article_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """Get a single article with full content, images, and provenance metadata.

    Vendor, product, and parent/top-level chapter are derived (the TOC is the
    source of truth), so they stay correct as the TOC is rebuilt across runs.
    """
    await authorize_article(db, principal, article_id, write=False)
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
async def get_toc(
    source_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """Get the table of contents for a source, with article IDs."""
    await authorize_source(db, principal, source_id, write=False)
    source_type = (await db.execute(
        select(DocumentationSource.source_type).where(DocumentationSource.id == source_id)
    )).scalar_one_or_none()
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

    # A PDF's TOC mirrors the PDF outline verbatim: many bookmark entries aren't
    # standalone articles but sub-sections that fold into the article covering their
    # page. Resolve those to that covering article (the most recent article-start in
    # reading order — entries are ordered by sort_order) so every node navigates to
    # the content that contains it. Web TOCs keep the strict semantics where a
    # non-article entry is a genuine grouping node (article_id stays None).
    if source_type == "pdf":
        covering: uuid.UUID | None = None
        for entry in entries:
            resp = entry_map[entry.id]
            if entry.id in article_toc_map:
                covering = article_toc_map[entry.id]
                resp.article_id = covering
            elif covering is not None:
                resp.article_id = covering
    else:
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
    principal: Principal = Depends(get_principal),
):
    """List an article's historical snapshots, newest first.

    Each ArticleVersion holds a *previous* content snapshot; the live content
    is on the Article itself (exposed here as ``current_hash``).
    """
    await authorize_article(db, principal, article_id, write=False)
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
    principal: Principal = Depends(get_principal),
):
    """Fetch a single version with its full content body (for side-by-side view)."""
    await authorize_article(db, principal, article_id, write=False)
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
    principal: Principal = Depends(get_principal),
):
    """Return the diff from a version's content to a newer state.

    A version stores the content that was *superseded*; its ``diff_text`` (when
    present) describes the transition to the content that replaced it. With
    ``against=next`` we return that stored diff when available, otherwise we
    compute one. ``against=current`` always diffs against the live article.
    """
    await authorize_article(db, principal, article_id, write=False)
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
