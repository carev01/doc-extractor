"""DocumentationSource CRUD routes."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.models.source import DocumentationSource
from app.schemas.source import (
    SourceCreate,
    SourceUpdate,
    SourceResponse,
    SourceListResponse,
)

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
