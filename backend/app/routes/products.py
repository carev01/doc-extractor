"""Product CRUD routes.

A product groups one or more documentation sources under a vendor. Deleting a
product cascades to its sources (and their articles/TOC/runs), same as deleting
a vendor cascades to its products.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.article import Article
from app.models.product import Product
from app.models.source import DocumentationSource
from app.models.vendor import Vendor
from app.schemas.product import (
    ProductCreate,
    ProductUpdate,
    ProductResponse,
    ProductListResponse,
)
from app.services.queue import ActiveRunExists, enqueue_run
from app.services.versioning import detect_version_token, derive_topic_key, resolve_template

router = APIRouter(prefix="/api/products", tags=["products"])


@router.post("", response_model=ProductResponse, status_code=201)
async def create_product(body: ProductCreate, db: AsyncSession = Depends(get_db)):
    """Create a new product under a vendor."""
    vendor = (
        await db.execute(select(Vendor).where(Vendor.id == body.vendor_id))
    ).scalar_one_or_none()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")

    product = Product(vendor_id=body.vendor_id, name=body.name)
    db.add(product)
    await db.commit()
    await db.refresh(product)
    return product


@router.get("", response_model=ProductListResponse)
async def list_products(
    vendor_id: uuid.UUID | None = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """List products, optionally filtered by vendor."""
    base_query = select(Product)
    count_query = select(func.count(Product.id))

    if vendor_id:
        base_query = base_query.where(Product.vendor_id == vendor_id)
        count_query = count_query.where(Product.vendor_id == vendor_id)

    total = (await db.execute(count_query)).scalar()
    result = await db.execute(
        base_query.order_by(Product.name).offset(skip).limit(limit)
    )
    products = result.scalars().all()

    return ProductListResponse(products=products, total=total)


@router.get("/{product_id}", response_model=ProductResponse)
async def get_product(product_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Get a product by ID."""
    product = (
        await db.execute(select(Product).where(Product.id == product_id))
    ).scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    return product


@router.patch("/{product_id}", response_model=ProductResponse)
async def update_product(
    product_id: uuid.UUID, body: ProductUpdate, db: AsyncSession = Depends(get_db)
):
    """Update a product (rename)."""
    product = (
        await db.execute(select(Product).where(Product.id == product_id))
    ).scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    if body.name is not None:
        product.name = body.name

    await db.commit()
    await db.refresh(product)
    return product


@router.delete("/{product_id}", status_code=204)
async def delete_product(product_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Delete a product and all its sources (cascades)."""
    product = (
        await db.execute(select(Product).where(Product.id == product_id))
    ).scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    await db.delete(product)
    await db.commit()


class _EnableVersionBody(BaseModel):
    version: str


@router.post("/{product_id}/versions/enable")
async def enable_versioning(
    product_id: uuid.UUID, body: _EnableVersionBody, db: AsyncSession = Depends(get_db)
):
    """Templatize child sources containing the version and rekey their articles."""
    product = await db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    sources = (
        await db.execute(
            select(DocumentationSource).where(
                DocumentationSource.product_id == product_id
            )
        )
    ).scalars().all()
    templatized = 0
    for src in sources:
        tmpl = detect_version_token(src.base_url, body.version)
        if tmpl is None:
            continue
        src.url_template = tmpl
        templatized += 1
        # Rekey existing articles so a later bump matches by version-independent key.
        arts = (
            await db.execute(select(Article).where(Article.source_id == src.id))
        ).scalars().all()
        for art in arts:
            art.topic_key = derive_topic_key(art.source_url, tmpl, body.version)
    product.version = body.version
    await db.commit()
    return {"version": product.version, "templatized_sources": templatized}


class _BumpVersionBody(BaseModel):
    version: str


@router.post("/{product_id}/versions/bump")
async def bump_version(
    product_id: uuid.UUID, body: _BumpVersionBody, db: AsyncSession = Depends(get_db)
):
    """Bump a product to a new version: rewrite templated source URLs and enqueue runs."""
    product = await db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    if not body.version or body.version == product.version:
        raise HTTPException(status_code=400, detail="Provide a new, different version")
    sources = (
        await db.execute(
            select(DocumentationSource).where(
                DocumentationSource.product_id == product_id,
                DocumentationSource.url_template.isnot(None),
            )
        )
    ).scalars().all()
    templated = [s for s in sources if "{version}" in (s.url_template or "")]
    if not templated:
        raise HTTPException(
            status_code=400, detail="No templated ({version}) sources to bump"
        )
    product.previous_version = product.version
    product.version = body.version
    for s in templated:
        s.base_url = resolve_template(s.url_template, body.version)
    await db.commit()

    run_ids = []
    for s in templated:
        try:
            run = await enqueue_run(db, s.id, trigger="version-bump")
            run_ids.append(str(run.id))
        except ActiveRunExists:
            continue  # a run is already queued/active for this source; skip
    return {"version": product.version, "runs": run_ids}
