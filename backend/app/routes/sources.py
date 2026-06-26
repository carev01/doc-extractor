"""DocumentationSource CRUD routes."""

import csv as csvlib
import io
import os
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel
from sqlalchemy import select, func, literal, union_all
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.database import get_db
from app.services.pdf_import import pdf_path_for
from app.models.article import Article
from app.models.article_version import ArticleVersion
from app.models.extraction_run import ExtractionRun, RunStatus
from app.models.job import Job
from app.models.product import Product
from app.models.source import DocumentationSource
from app.models.toc import TOCEntry
from app.models.vendor import Vendor
from app.schemas.browse import BrowseTOCEntry, RemovedArticle, BrowseResponse
from app.schemas.source import (
    SourceCreate,
    SourceUpdate,
    SourceResponse,
    SourceListResponse,
    PickableSource,
    PickableSourceList,
    SourceImportRequest,
    SourceImportRow,
    SourceImportResult,
)
from app.schemas.version import ChangelogEntry, ChangelogResponse
from app.services.versioning import detect_version_token, resolve_template

router = APIRouter(prefix="/api/sources", tags=["sources"])


@router.post("", response_model=SourceResponse, status_code=201)
async def create_source(body: SourceCreate, db: AsyncSession = Depends(get_db)):
    """Add a new documentation source to extract, under a product."""
    product = (
        await db.execute(select(Product).where(Product.id == body.product_id))
    ).scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    base_url = body.base_url
    if body.url_template and product.version:
        base_url = resolve_template(body.url_template, product.version)

    source = DocumentationSource(
        product_id=body.product_id,
        name=body.name,
        base_url=base_url,
        url_template=body.url_template,
    )
    db.add(source)
    await db.commit()
    await db.refresh(source)
    return source


@router.get("", response_model=SourceListResponse)
async def list_sources(
    product_id: uuid.UUID | None = Query(None),
    vendor_id: uuid.UUID | None = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """List documentation sources, optionally filtered by product or vendor.

    ``product_id`` filters directly; ``vendor_id`` filters via the source's
    product (so "all sources for a vendor" still resolves under the new nesting).
    """
    base_query = select(DocumentationSource)
    count_query = select(func.count(DocumentationSource.id))

    if product_id:
        base_query = base_query.where(DocumentationSource.product_id == product_id)
        count_query = count_query.where(DocumentationSource.product_id == product_id)

    if vendor_id:
        vendor_products = (
            select(Product.id).where(Product.vendor_id == vendor_id).scalar_subquery()
        )
        base_query = base_query.where(DocumentationSource.product_id.in_(vendor_products))
        count_query = count_query.where(
            DocumentationSource.product_id.in_(vendor_products)
        )

    total_result = await db.execute(count_query)
    total = total_result.scalar()

    result = await db.execute(
        base_query.order_by(DocumentationSource.name).offset(skip).limit(limit)
    )
    sources = result.scalars().all()

    return SourceListResponse(sources=sources, total=total)


@router.get("/pickable", response_model=PickableSourceList)
async def list_pickable_sources(db: AsyncSession = Depends(get_db)):
    """All sources with vendor/product labels and their current job (if any),
    for the job view's source picker."""
    rows = (
        await db.execute(
            select(
                DocumentationSource.id,
                DocumentationSource.name,
                Vendor.name.label("vendor_name"),
                Product.name.label("product_name"),
                DocumentationSource.job_id,
                Job.name.label("job_name"),
            )
            .join(Product, DocumentationSource.product_id == Product.id)
            .join(Vendor, Product.vendor_id == Vendor.id)
            .outerjoin(Job, DocumentationSource.job_id == Job.id)
            .order_by(Vendor.name, Product.name, DocumentationSource.name)
        )
    ).all()
    return PickableSourceList(sources=[
        PickableSource(
            id=r.id, name=r.name, vendor_name=r.vendor_name,
            product_name=r.product_name, job_id=r.job_id, job_name=r.job_name,
        )
        for r in rows
    ])


REQUIRED_COLUMNS = {"vendor", "product", "source_name", "base_url"}


@router.post("/import", response_model=SourceImportResult)
async def import_sources(body: SourceImportRequest, db: AsyncSession = Depends(get_db)):
    """Bulk-import sources from CSV. Auto-creates vendors/products by name;
    skips a source when (product, base_url) already exists."""
    reader = csvlib.DictReader(io.StringIO(body.csv))
    if reader.fieldnames is None or not REQUIRED_COLUMNS.issubset(
        {(f or "").strip().lower() for f in reader.fieldnames}
    ):
        raise HTTPException(
            status_code=422,
            detail=f"CSV must have columns: {', '.join(sorted(REQUIRED_COLUMNS))}",
        )

    # In-request caches keyed by lowercased trimmed names.
    vendor_cache: dict[str, Vendor] = {}
    product_cache: dict[tuple[str, str], Product] = {}
    rows: list[SourceImportRow] = []
    created = skipped = errors = 0

    async def _vendor(name: str) -> Vendor:
        key = name.lower()
        if key in vendor_cache:
            return vendor_cache[key]
        existing = (
            await db.execute(
                select(Vendor).where(func.lower(Vendor.name) == key)
            )
        ).scalar_one_or_none()
        if existing is None:
            existing = Vendor(name=name)
            db.add(existing)
            await db.flush()
        vendor_cache[key] = existing
        return existing

    async def _product(vendor: Vendor, name: str) -> Product:
        key = (str(vendor.id), name.lower())
        if key in product_cache:
            return product_cache[key]
        existing = (
            await db.execute(
                select(Product).where(
                    Product.vendor_id == vendor.id,
                    func.lower(Product.name) == name.lower(),
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            existing = Product(vendor_id=vendor.id, name=name)
            db.add(existing)
            await db.flush()
        product_cache[key] = existing
        return existing

    # Row numbers start at 2 (row 1 is the header).
    for i, raw in enumerate(reader, start=2):
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        vendor_name = row.get("vendor", "")
        product_name = row.get("product", "")
        source_name = row.get("source_name", "")
        base_url = row.get("base_url", "")
        url_template = row.get("url_template", "") or None

        missing = [
            c for c, val in (
                ("vendor", vendor_name), ("product", product_name),
                ("source_name", source_name), ("base_url", base_url),
            ) if not val
        ]
        if missing:
            errors += 1
            rows.append(SourceImportRow(
                row=i, result="error", vendor=vendor_name or None,
                product=product_name or None, source_name=source_name or None,
                message=f"missing required value(s): {', '.join(missing)}",
            ))
            continue

        vendor = await _vendor(vendor_name)
        product = await _product(vendor, product_name)

        dup = (
            await db.execute(
                select(DocumentationSource.id).where(
                    DocumentationSource.product_id == product.id,
                    DocumentationSource.base_url == base_url,
                )
            )
        ).scalar_one_or_none()
        if dup is not None:
            skipped += 1
            rows.append(SourceImportRow(
                row=i, result="skipped", vendor=vendor_name,
                product=product_name, source_name=source_name,
                message="source with this base_url already exists",
            ))
            continue

        db.add(DocumentationSource(
            product_id=product.id, name=source_name,
            base_url=base_url, url_template=url_template,
        ))
        created += 1
        rows.append(SourceImportRow(
            row=i, result="created", vendor=vendor_name,
            product=product_name, source_name=source_name,
        ))

    await db.commit()
    return SourceImportResult(
        created=created, skipped=skipped, errors=errors, rows=rows,
    )


@router.post("/pdf", response_model=SourceResponse, status_code=201)
async def create_pdf_source(
    product_id: uuid.UUID = Form(...),
    name: str = Form(...),
    pdf_url: str | None = Form(None),
    file: UploadFile | None = File(None),
    db: AsyncSession = Depends(get_db),
):
    """Create a PDF source from either a URL (re-fetchable) or an uploaded file."""
    product = (
        await db.execute(select(Product).where(Product.id == product_id))
    ).scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    if bool(pdf_url) == bool(file):
        raise HTTPException(status_code=422, detail="Provide exactly one of pdf_url or file")

    if pdf_url:
        source = DocumentationSource(
            product_id=product_id, name=name, base_url=pdf_url, source_type="pdf",
        )
        db.add(source)
        await db.commit()
        await db.refresh(source)
        return source

    # Upload path.
    if file.content_type not in ("application/pdf", "application/x-pdf"):
        raise HTTPException(status_code=415, detail="File must be a PDF")
    data = await file.read()
    if len(data) > settings.pdf_max_upload_bytes:
        raise HTTPException(status_code=413, detail="PDF exceeds the maximum upload size")

    source = DocumentationSource(
        product_id=product_id, name=name, base_url="pending", source_type="pdf",
    )
    db.add(source)
    await db.flush()
    source.base_url = f"file://{source.id}.pdf"
    os.makedirs(settings.pdf_dir, exist_ok=True)
    with open(pdf_path_for(source.id, settings.pdf_dir), "wb") as fh:
        fh.write(data)
    await db.commit()
    await db.refresh(source)
    return source


@router.put("/{source_id}/pdf", response_model=SourceResponse)
async def replace_pdf_file(
    source_id: uuid.UUID,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    """Replace the stored file for an upload-origin PDF source."""
    source = (
        await db.execute(select(DocumentationSource).where(DocumentationSource.id == source_id))
    ).scalar_one_or_none()
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")
    if source.source_type != "pdf" or not source.base_url.startswith("file://"):
        raise HTTPException(status_code=409, detail="Not an upload-origin PDF source")
    if file.content_type not in ("application/pdf", "application/x-pdf"):
        raise HTTPException(status_code=415, detail="File must be a PDF")
    data = await file.read()
    if len(data) > settings.pdf_max_upload_bytes:
        raise HTTPException(status_code=413, detail="PDF exceeds the maximum upload size")
    os.makedirs(settings.pdf_dir, exist_ok=True)
    with open(pdf_path_for(source.id, settings.pdf_dir), "wb") as fh:
        fh.write(data)
    await db.commit()
    await db.refresh(source)
    return source


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
    if body.product_id is not None and body.product_id != source.product_id:
        # Move the source to another product (must exist).
        target = (
            await db.execute(select(Product).where(Product.id == body.product_id))
        ).scalar_one_or_none()
        if not target:
            raise HTTPException(status_code=404, detail="Target product not found")
        source.product_id = body.product_id
    if body.url_template is not None:
        source.url_template = body.url_template
        # Resolve base_url from the new template against the source's EFFECTIVE
        # product (i.e. the new product if product_id was just reassigned above).
        product = (
            await db.execute(select(Product).where(Product.id == source.product_id))
        ).scalar_one_or_none()
        if product and product.version:
            source.base_url = resolve_template(body.url_template, product.version)
    if body.platform is not None:
        # "" / "auto" clears the override so detection runs again next extraction.
        source.platform = None if body.platform in ("", "auto") else body.platform

    if body.refresh_profile and source.profile_config:
        # Drop the cached LLM-derived spec so the next extraction re-derives it.
        # New-dict assignment so SQLAlchemy detects the JSONB change.
        remaining = {k: v for k, v in source.profile_config.items() if k != "llm_spec"}
        source.profile_config = remaining or None

    await db.commit()
    await db.refresh(source)
    return source


class _DetectTokenBody(BaseModel):
    version: str


@router.post("/{source_id}/detect-version-token")
async def detect_version_token_route(
    source_id: uuid.UUID, body: _DetectTokenBody, db: AsyncSession = Depends(get_db)
):
    """Return a url_template by detecting the version token in the source's base_url."""
    source = await db.get(DocumentationSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    return {"url_template": detect_version_token(source.base_url, body.version)}


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

    # The baseline run is the earliest run that actually created articles for this
    # source (a failed first run that created nothing is skipped). Its pages are
    # collapsed into a single "initial" summary instead of one "added" per page.
    baseline_row = (
        await db.execute(
            select(
                Article.created_run_id,
                ExtractionRun.started_at,
                ExtractionRun.completed_at,
            )
            .join(ExtractionRun, ExtractionRun.id == Article.created_run_id)
            .where(Article.source_id == source_id)
            .order_by(ExtractionRun.started_at.asc())
            .limit(1)
        )
    ).first()
    baseline_run_id = baseline_row.created_run_id if baseline_row else None
    baseline_time = (
        (baseline_row.completed_at or baseline_row.started_at) if baseline_row else None
    )
    baseline_count = 0
    if baseline_run_id is not None:
        baseline_count = (
            await db.execute(
                select(func.count())
                .select_from(Article)
                .where(
                    Article.source_id == source_id,
                    Article.created_run_id == baseline_run_id,
                )
            )
        ).scalar()

    # Typed NULLs so all union branches agree on column types (an untyped NULL
    # can trip Postgres' UNION type resolution).
    null_version_id = literal(None).cast(ArticleVersion.id.type)
    null_article_id = literal(None).cast(Article.id.type)

    # 'added' — only pages added AFTER the baseline run (baseline is summarised).
    added = select(
        Article.id.label("article_id"),
        Article.title.label("title"),
        literal("added").label("change_type"),
        Article.created_at.label("timestamp"),
        null_version_id.label("version_id"),
        Article.created_run_id.label("extraction_run_id"),
        literal(False).label("has_diff"),
    ).where(Article.source_id == source_id, Article.created_run_id.isnot(None))
    if baseline_run_id is not None:
        added = added.where(Article.created_run_id != baseline_run_id)

    changed = select(
        ArticleVersion.article_id.label("article_id"),
        Article.title.label("title"),
        literal("changed").label("change_type"),
        ArticleVersion.extracted_at.label("timestamp"),
        ArticleVersion.id.label("version_id"),
        ArticleVersion.extraction_run_id.label("extraction_run_id"),
        ArticleVersion.diff_text.isnot(None).label("has_diff"),
    ).join(Article, Article.id == ArticleVersion.article_id).where(
        Article.source_id == source_id
    )

    removed = select(
        Article.id.label("article_id"),
        Article.title.label("title"),
        literal("removed").label("change_type"),
        Article.removed_at.label("timestamp"),
        null_version_id.label("version_id"),
        Article.removal_run_id.label("extraction_run_id"),
        literal(False).label("has_diff"),
    ).where(Article.source_id == source_id, Article.removed_at.isnot(None))

    parts = [added, changed, removed]
    if baseline_run_id is not None and baseline_count > 0:
        # One synthetic summary row for the baseline extraction.
        initial = select(
            null_article_id.label("article_id"),
            literal(
                f"Initial extraction — {baseline_count} articles added"
            ).label("title"),
            literal("initial").label("change_type"),
            literal(baseline_time).cast(Article.created_at.type).label("timestamp"),
            null_version_id.label("version_id"),
            literal(baseline_run_id).cast(Article.id.type).label("extraction_run_id"),
            literal(False).label("has_diff"),
        )
        parts.append(initial)

    events = union_all(*parts).subquery()
    total = (await db.execute(select(func.count()).select_from(events))).scalar()

    rows_q = (
        select(events, ExtractionRun.version.label("run_version"))
        .select_from(events)
        .outerjoin(ExtractionRun, ExtractionRun.id == events.c.extraction_run_id)
        .order_by(events.c.timestamp.desc())
        .offset(skip)
        .limit(limit)
    )
    rows = (await db.execute(rows_q)).all()

    entries = [
        ChangelogEntry(
            article_id=r.article_id,
            title=r.title,
            change_type=r.change_type,
            timestamp=r.timestamp,
            version_id=r.version_id,
            extraction_run_id=r.extraction_run_id,
            version=r.run_version,
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
