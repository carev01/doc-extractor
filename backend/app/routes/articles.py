"""Article query routes."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.models.article import Article
from app.models.article_version import ArticleVersion
from app.models.extraction_run import ExtractionRun
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


@router.get("", response_model=ArticleListResponse)
async def list_articles(
    source_id: uuid.UUID | None = Query(None),
    toc_entry_id: uuid.UUID | None = Query(None),
    search: str | None = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """List articles with optional filtering."""
    base_query = select(Article)
    count_query = select(func.count(Article.id))

    if source_id:
        base_query = base_query.where(Article.source_id == source_id)
        count_query = count_query.where(Article.source_id == source_id)
    if toc_entry_id:
        base_query = base_query.where(Article.toc_entry_id == toc_entry_id)
        count_query = count_query.where(Article.toc_entry_id == toc_entry_id)
    if search:
        base_query = base_query.where(Article.title.ilike(f"%{search}%"))
        count_query = count_query.where(Article.title.ilike(f"%{search}%"))

    total_result = await db.execute(count_query)
    total = total_result.scalar()

    result = await db.execute(
        base_query.order_by(Article.sort_order).offset(skip).limit(limit)
    )
    articles = result.scalars().all()

    return ArticleListResponse(articles=articles, total=total)


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
