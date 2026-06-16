"""Article query routes."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.models.article import Article
from app.models.toc import TOCEntry
from app.schemas.article import (
    ArticleResponse,
    ArticleDetailResponse,
    ArticleListResponse,
    TOCEntryResponse,
    TOCResponse,
)

router = APIRouter(prefix="/api/articles", tags=["articles"])


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
    """Get a single article with full content and images."""
    result = await db.execute(
        select(Article)
        .where(Article.id == article_id)
        .options(selectinload(Article.images))
    )
    article = result.scalar_one_or_none()
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")
    return article


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
