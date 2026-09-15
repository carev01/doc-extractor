"""Product CRUD routes.

A product groups one or more documentation sources under a vendor. Deleting a
product cascades to its sources (and their articles/TOC/runs), same as deleting
a vendor cascades to its products. Access follows the owning vendor: read needs
a grant, mutations need a read_write grant (admins bypass).
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authz import (
    Principal, authorize_product, get_principal, require_vendor_read, require_vendor_write,
)
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
from app.services import change_log
from app.services.queue import ActiveRunExists, enqueue_run
from app.services.profiles import registry as profile_registry
from app.services.versioning import (
    REVISION_PLACEHOLDER,
    derive_topic_key,
    extract_revision,
    resolve_template,
    templatize,
)

router = APIRouter(prefix="/api/products", tags=["products"])


@router.post("", response_model=ProductResponse, status_code=201)
async def create_product(
    body: ProductCreate,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
):
    """Create a new product under a vendor (needs read_write on the vendor)."""
    require_vendor_write(principal, body.vendor_id)
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
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
):
    """List products under vendors the caller can see."""
    base_query = select(Product)
    count_query = select(func.count(Product.id))

    if vendor_id:
        require_vendor_read(principal, vendor_id)
        base_query = base_query.where(Product.vendor_id == vendor_id)
        count_query = count_query.where(Product.vendor_id == vendor_id)
    else:
        visible = principal.visible_vendor_ids()  # None = all
        if visible is not None:
            if not visible:
                return ProductListResponse(products=[], total=0)
            base_query = base_query.where(Product.vendor_id.in_(visible))
            count_query = count_query.where(Product.vendor_id.in_(visible))

    total = (await db.execute(count_query)).scalar()
    result = await db.execute(base_query.order_by(Product.name).offset(skip).limit(limit))
    return ProductListResponse(products=result.scalars().all(), total=total)


@router.get("/{product_id}", response_model=ProductResponse)
async def get_product(
    product_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
):
    """Get a product by ID (404 if its vendor isn't visible)."""
    await authorize_product(db, principal, product_id, write=False)
    product = (
        await db.execute(select(Product).where(Product.id == product_id))
    ).scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    return product


@router.patch("/{product_id}", response_model=ProductResponse)
async def update_product(
    product_id: uuid.UUID,
    body: ProductUpdate,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
):
    """Update a product (rename)."""
    await authorize_product(db, principal, product_id, write=True)
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
async def delete_product(
    product_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
):
    """Delete a product and all its sources (cascades)."""
    await authorize_product(db, principal, product_id, write=True)
    product = (
        await db.execute(select(Product).where(Product.id == product_id))
    ).scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    source_ids = (await db.execute(
        select(DocumentationSource.id).where(DocumentationSource.product_id == product_id)
    )).scalars().all()
    await change_log.record_source_deletions(db, source_ids=source_ids)
    await db.delete(product)
    await db.commit()


class _EnableVersionBody(BaseModel):
    version: str


@router.post("/{product_id}/versions/enable")
async def enable_versioning(
    product_id: uuid.UUID,
    body: _EnableVersionBody,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
):
    """Templatize child sources containing the version and rekey their articles."""
    await authorize_product(db, principal, product_id, write=True)
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
        # Profile-aware so a platform with a per-release token (Cohesity's build
        # id) gets a {rev} template, not a {version}-only one that would re-key
        # every article at the next release.
        tmpl = templatize(
            src.base_url, body.version, profile_registry.get(src.platform or "")
        )
        if tmpl is None:
            continue
        src.url_template = tmpl
        templatized += 1
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
    # Proceed even when a {rev} token could not be resolved. Mirrors the
    # per-run allow_toc_collapse override: the operator has seen the numbers and
    # accepts the risk, rather than the system deciding for them.
    force: bool = False


async def _plan_bump(sources, version: str) -> list[dict]:
    """Work out what each templated source's base_url becomes at *version*.

    A ``{version}``-only template is a pure substitution and is reported as
    ``ok``. A ``{rev}`` template needs the vendor's current per-release token,
    which only the profile can look up; that lookup is reported honestly as
    ``resolved`` or ``unresolved`` rather than being folded into a URL string the
    operator would have no way to tell apart from a working one.

    Unresolved is not fatal: the carried-forward token keeps base_url well-formed
    and the next run re-resolves it. It is surfaced so a bump isn't committed
    blind, because the bump is what overwrites ``previous_version``.
    """
    from app.services.firecrawl import firecrawl_service
    from app.services.profiles.scraper import Scraper

    plan: list[dict] = []
    for s in sources:
        template = s.url_template or ""
        entry = {
            "source_id": str(s.id),
            "name": s.name,
            "current_url": s.base_url,
            "url_template": template,
            "revision": None,
            "status": "ok",
            "detail": None,
        }
        if REVISION_PLACEHOLDER not in template:
            entry["resolved_url"] = resolve_template(template, version)
            plan.append(entry)
            continue

        profile = profile_registry.get(s.platform or "")
        resolver = getattr(profile, "resolve_revision", None) if profile else None
        revision = None
        if resolver is not None:
            try:
                revision = await resolver(template, version, Scraper(firecrawl_service))
            except Exception as exc:
                entry["detail"] = str(exc)

        if revision:
            entry["status"] = "resolved"
            entry["revision"] = revision
        else:
            entry["status"] = "unresolved"
            # Carry the release token the source is on now, so base_url stays a
            # well-formed URL instead of one with a literal "{rev}" in it. Read
            # at the version base_url currently carries, which is the OLD one.
            revision = extract_revision(
                s.base_url, template, _version_in(s.base_url, template)
            )
            entry["detail"] = entry["detail"] or (
                f"No {REVISION_PLACEHOLDER} found for {version}"
                + ("" if profile else " (source has no platform profile)")
            )
        # Never emit a half-resolved URL: with no token at all (base_url doesn't
        # match its own template — a misconfiguration) an empty substitution
        # would yield a plausible-looking but malformed URL, so leave the source
        # exactly where it is and let the "not found" status carry the news.
        entry["resolved_url"] = (
            resolve_template(template, version, revision) if revision else s.base_url
        )
        plan.append(entry)
    return plan


def _version_in(url: str, template: str) -> str | None:
    """The version segment *url* currently carries, read at the template's
    ``{version}`` offset — needed to locate ``{rev}`` in a URL whose version is
    the *old* one."""
    head = template.split("{version}", 1)[0]
    if not url.startswith(head):
        return None
    rest = url[len(head):]
    return rest.split("/", 1)[0] or None


@router.post("/{product_id}/versions/bump")
async def bump_version(
    product_id: uuid.UUID,
    body: _BumpVersionBody,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
):
    """Bump a product to a new version: rewrite templated source URLs and enqueue runs."""
    await authorize_product(db, principal, product_id, write=True)
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
    plan = await _plan_bump(templated, body.version)
    blocked = [e for e in plan if e["status"] == "unresolved"]
    if blocked and not body.force:
        # Refuse BEFORE touching previous_version: a bump that half-lands leaves
        # no record of where the product came from, and the operator would be
        # re-running against a URL the vendor 404s.
        raise HTTPException(
            status_code=409,
            detail={
                "message": (
                    f"{len(blocked)} source(s) have an unresolved {{rev}} token "
                    f"for {body.version}. Re-check, or bump with force=true to "
                    f"proceed and let the next run resolve it."
                ),
                "sources": blocked,
            },
        )

    by_id = {e["source_id"]: e for e in plan}
    product.previous_version = product.version
    product.version = body.version
    for s in templated:
        s.base_url = by_id[str(s.id)]["resolved_url"]
    await db.commit()

    run_ids = []
    for s in templated:
        try:
            run = await enqueue_run(db, s.id, trigger="version-bump")
            run_ids.append(str(run.id))
        except ActiveRunExists:
            continue
    return {"version": product.version, "runs": run_ids, "sources": plan}


@router.post("/{product_id}/versions/preview")
async def preview_version_bump(
    product_id: uuid.UUID,
    body: _BumpVersionBody,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
):
    """Dry-run a bump: what each templated source's URL becomes, and whether the
    vendor's per-release token could be resolved. Changes nothing."""
    await authorize_product(db, principal, product_id, write=True)
    product = await db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    sources = (
        await db.execute(
            select(DocumentationSource).where(
                DocumentationSource.product_id == product_id,
                DocumentationSource.url_template.isnot(None),
            )
        )
    ).scalars().all()
    templated = [s for s in sources if "{version}" in (s.url_template or "")]
    return {
        "version": product.version,
        "target_version": body.version,
        "sources": await _plan_bump(templated, body.version),
    }
