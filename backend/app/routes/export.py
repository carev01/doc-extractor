"""Export routes — async export enqueue, job status, and file download."""

import contextlib
import os
import shutil
import zipfile
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authz import (
    Principal, authorize_product, authorize_source, get_principal, require_admin,
)
from app.core.config import settings
from app.core.database import get_db
from app.models.article import Article
from app.models.export_job import ExportJob, ExportStatus
from app.models.image import ArticleImage
from app.models.product import Product
from app.models.source import DocumentationSource
from app.models.vendor import Vendor
from app.schemas.export import (
    BatchSourceStatus,
    ExportBatchResponse,
    ExportJobCreatedResponse,
    ExportJobStatusResponse,
    ExportRequest,
    ProductExportCreatedResponse,
    ProductExportPreview,
    ProductExportRequest,
)
from app.services.exporter import export_engine
from app.services.queue import enqueue_export, enqueue_export_batch

router = APIRouter(prefix="/api/export", tags=["export"])


@router.post("", response_model=ExportJobCreatedResponse)
async def create_export(
    body: ExportRequest,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """Enqueue an export job; the worker generates it. Poll /api/export/jobs/{id}."""
    src = await db.execute(
        select(DocumentationSource.id).where(DocumentationSource.id == body.source_id)
    )
    if src.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Source not found")
    await authorize_source(db, principal, body.source_id, write=False)
    job = await enqueue_export(db, body.source_id, body.model_dump(mode="json"))
    return ExportJobCreatedResponse(export_job_id=job.id, status="pending")


@router.get("/jobs")
async def list_export_jobs(
    status: str | None = None,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """List export jobs (newest first) with vendor/product/source names.

    Powers the Exports section of the Jobs view. Optionally filter by status.
    """
    limit = max(1, min(limit, 500))

    visible = principal.visible_vendor_ids()
    if visible is not None and not visible:
        return {"jobs": []}

    query = (
        select(
            ExportJob,
            DocumentationSource.name.label("source_name"),
            Product.name.label("product_name"),
            Vendor.name.label("vendor_name"),
        )
        .join(DocumentationSource, ExportJob.source_id == DocumentationSource.id)
        .join(Product, DocumentationSource.product_id == Product.id)
        .join(Vendor, Product.vendor_id == Vendor.id)
        .order_by(ExportJob.created_at.desc())
    )
    if status:
        query = query.where(ExportJob.status == status)
    if visible is not None:
        query = query.where(Product.vendor_id.in_(visible))
    rows = (await db.execute(query.limit(limit))).all()
    return {
        "jobs": [
            {
                "id": j.id,
                "source_id": j.source_id,
                "source_name": source_name,
                "product_name": product_name,
                "vendor_name": vendor_name,
                "status": j.status.value,
                "format": (j.request or {}).get("format", "markdown"),
                "attempts": j.attempts,
                "export_id": j.export_id,
                "error_message": j.error_message,
                "created_at": j.created_at.isoformat() if j.created_at else None,
                "started_at": j.started_at.isoformat() if j.started_at else None,
                "completed_at": j.completed_at.isoformat() if j.completed_at else None,
            }
            for j, source_name, product_name, vendor_name in rows
        ]
    }


@router.post("/jobs/{job_id}/cancel")
async def cancel_export_job(
    job_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """Cancel a *queued* export job. Running jobs can't be cancelled (one-shot
    generation); they finish or fail on their own."""
    job = (await db.execute(select(ExportJob).where(ExportJob.id == job_id))).scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="Export job not found")
    await authorize_source(db, principal, job.source_id, write=False)
    if job.status != ExportStatus.PENDING:
        raise HTTPException(
            status_code=409,
            detail=f"Only queued export jobs can be cancelled (status={job.status.value})",
        )
    job.status = ExportStatus.CANCELLED
    await db.commit()
    return {"id": job.id, "status": job.status.value}


@router.get("/jobs/{job_id}", response_model=ExportJobStatusResponse)
async def get_export_job(
    job_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    job = (await db.execute(select(ExportJob).where(ExportJob.id == job_id))).scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="Export job not found")
    await authorize_source(db, principal, job.source_id, write=False)
    result = job.result or {}
    return ExportJobStatusResponse(
        id=job.id, source_id=job.source_id, status=job.status.value,
        export_id=job.export_id, zip_filename=result.get("zip_filename"),
        files=result.get("files"), error_message=job.error_message,
    )


@router.get("/download/{export_id}")
async def download_export_zip(
    export_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """Download the self-contained zip bundle (markdown + images) for an export."""
    job = (
        await db.execute(select(ExportJob).where(ExportJob.export_id == export_id))
    ).scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="Export not found")
    await authorize_source(db, principal, job.source_id, write=False)

    export_subdir = os.path.join(export_engine.export_dir, str(export_id))
    if not os.path.isdir(export_subdir):
        raise HTTPException(status_code=404, detail="Export not found")

    zips = [f for f in os.listdir(export_subdir) if f.endswith(".zip")]
    if not zips:
        raise HTTPException(status_code=404, detail="Export bundle not found")

    return FileResponse(
        os.path.join(export_subdir, zips[0]),
        media_type="application/zip",
        filename=zips[0],
    )


@router.get("/download/{export_id}/{filename}")
async def download_export_file(
    export_id: uuid.UUID,
    filename: str,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """Download a specific export file."""
    job = (
        await db.execute(select(ExportJob).where(ExportJob.export_id == export_id))
    ).scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="Export file not found")
    await authorize_source(db, principal, job.source_id, write=False)

    filepath = os.path.join(
        export_engine.export_dir, str(export_id), filename
    )

    if not os.path.isfile(filepath):
        raise HTTPException(status_code=404, detail="Export file not found")

    # Security: prevent path traversal
    real_path = os.path.realpath(filepath)
    real_export_dir = os.path.realpath(
        os.path.join(export_engine.export_dir, str(export_id))
    )
    if not real_path.startswith(real_export_dir):
        raise HTTPException(status_code=403, detail="Access denied")

    media_type = "application/pdf" if filename.lower().endswith(".pdf") else "text/markdown"
    return FileResponse(
        real_path,
        media_type=media_type,
        filename=filename,
    )


@router.get("/list")
async def list_exports(
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """List recent (non-expired) completed exports with metadata, newest first.

    Backed by export_jobs (the source of truth) so the listing survives page
    navigation and includes timestamp/source/format. Retention purges old exports;
    rows whose on-disk directory is gone are skipped.
    """
    from datetime import timedelta

    visible = principal.visible_vendor_ids()
    if visible is not None and not visible:
        return {"exports": []}

    query = (
        select(ExportJob, DocumentationSource.name)
        .join(DocumentationSource, ExportJob.source_id == DocumentationSource.id)
        .join(Product, DocumentationSource.product_id == Product.id)
        .where(
            ExportJob.status == ExportStatus.COMPLETED,
            ExportJob.export_id.isnot(None),
        )
        .order_by(ExportJob.created_at.desc())
        .limit(20)
    )
    if visible is not None:
        query = query.where(Product.vendor_id.in_(visible))

    rows = (await db.execute(query)).all()

    retention_days = settings.export_retention_days
    exports = []
    for job, source_name in rows:
        subdir = os.path.join(export_engine.export_dir, str(job.export_id))
        if not os.path.isdir(subdir):
            continue  # purged out-of-band
        result = job.result or {}
        expires_at = (
            (job.created_at + timedelta(days=retention_days)).isoformat()
            if retention_days > 0 else None
        )
        exports.append({
            "export_id": str(job.export_id),
            "source_id": str(job.source_id),
            "source_name": source_name,
            "format": (job.request or {}).get("format", "markdown"),
            "created_at": job.created_at.isoformat(),
            "expires_at": expires_at,
            "file_count": result.get("file_count", 0),
            "files": [f["filename"] for f in result.get("files", [])],
            "zip_filename": result.get("zip_filename"),
            "total_size_bytes": result.get("total_size_bytes", 0),
        })

    return {"exports": exports}


@router.delete("/{export_id}", status_code=204)
async def delete_export(
    export_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """Delete a generated export now — its on-disk directory and its export_jobs
    row — so a user can reclaim space without waiting for the retention sweep.

    Idempotent: removing the directory and row together keeps the listing and the
    filesystem consistent (the same invariant retention relies on). 404 only when
    no matching export exists at all.
    """
    job = (
        await db.execute(
            select(ExportJob).where(ExportJob.export_id == export_id)
        )
    ).scalar_one_or_none()

    subdir = os.path.join(export_engine.export_dir, str(export_id))
    dir_exists = os.path.isdir(subdir)
    if job is None and not dir_exists:
        raise HTTPException(status_code=404, detail="Export not found")

    if job is not None:
        await authorize_source(db, principal, job.source_id, write=False)
    else:
        # Orphaned directory with no export_jobs row → no vendor to check
        # against; only an admin may reclaim it.
        require_admin(principal)

    if dir_exists:
        shutil.rmtree(subdir, ignore_errors=True)
    if job is not None:
        await db.delete(job)
        await db.commit()
    return None


# ---------------------------------------------------------------------------
# Product-level (batch) export
# ---------------------------------------------------------------------------
#
# A product export fans out into one ordinary per-source ExportJob each, sharing
# a batch_id. The export engine is untouched: every job is exactly the job a
# single-source export already produces. See the f9a0b1c2d3e4 migration for why
# fan-out beats one product-scoped job.


async def _product_label(db: AsyncSession, product: Product) -> str:
    vendor = (
        await db.execute(select(Vendor.name).where(Vendor.id == product.vendor_id))
    ).scalar_one_or_none()
    return f"{vendor} / {product.name}" if vendor else product.name


async def _exportable_sources(db: AsyncSession, product_id: uuid.UUID, only: list[uuid.UUID] | None):
    """Sources of *product_id* that have at least one live article, plus the
    names of those skipped for having none.

    Skipping is not cosmetic: ``export_sync`` raises "No articles matched the
    selection criteria" for an empty source, so including one would put a
    permanent FAILED row in the batch that the operator can do nothing about.
    """
    rows = (
        await db.execute(
            select(
                DocumentationSource.id,
                DocumentationSource.name,
                func.count(Article.id).label("live"),
            )
            .outerjoin(
                Article,
                (Article.source_id == DocumentationSource.id)
                & (Article.removed_at.is_(None)),
            )
            .where(DocumentationSource.product_id == product_id)
            .group_by(DocumentationSource.id, DocumentationSource.name)
            .order_by(DocumentationSource.name)
        )
    ).all()
    wanted = set(only or [])
    keep, skipped = [], []
    for sid, name, live in rows:
        if wanted and sid not in wanted:
            continue
        (keep if live else skipped).append((sid, name))
    return keep, [n for _sid, n in skipped]


@router.get("/product/{product_id}/preview", response_model=ProductExportPreview)
async def preview_product_export(
    product_id: uuid.UUID,
    include_images: bool = False,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """Project what a product export would generate, before generating it.

    Image payloads dwarf the text — Veeam Backup & Replication is 27 MB of
    markdown against 495 MB of images — and the exports volume is finite, so the
    size is worth seeing before the worker is committed to producing it.
    """
    await authorize_product(db, principal, product_id, write=False)
    product = await db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")

    keep, skipped = await _exportable_sources(db, product_id, None)
    keep_ids = [sid for sid, _n in keep]

    articles = md_bytes = image_bytes = 0
    if keep_ids:
        articles, md_bytes = (
            await db.execute(
                select(
                    func.count(Article.id),
                    func.coalesce(func.sum(func.length(Article.content_markdown)), 0),
                ).where(
                    Article.source_id.in_(keep_ids), Article.removed_at.is_(None)
                )
            )
        ).one()
        image_bytes = (
            await db.execute(
                select(func.coalesce(func.sum(ArticleImage.file_size_bytes), 0))
                .select_from(ArticleImage)
                .join(Article, Article.id == ArticleImage.article_id)
                .where(
                    Article.source_id.in_(keep_ids), Article.removed_at.is_(None)
                )
            )
        ).scalar_one()

    return ProductExportPreview(
        product_id=product_id,
        label=await _product_label(db, product),
        source_count=len(keep) + len(skipped),
        exportable_source_count=len(keep),
        skipped=skipped,
        total_articles=int(articles or 0),
        markdown_bytes=int(md_bytes or 0),
        image_bytes=int(image_bytes or 0),
        projected_bytes=int(md_bytes or 0) + (int(image_bytes or 0) if include_images else 0),
    )


@router.post("/product/{product_id}", response_model=ProductExportCreatedResponse)
async def create_product_export(
    product_id: uuid.UUID,
    body: ProductExportRequest,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """Enqueue one export job per source of a product, tied together by a batch id."""
    await authorize_product(db, principal, product_id, write=False)
    product = await db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")

    keep, skipped = await _exportable_sources(db, product_id, body.source_ids)
    if not keep:
        raise HTTPException(
            status_code=400,
            detail=(
                "No exportable sources: this product's sources hold no articles yet."
                if not skipped
                else f"No exportable sources ({len(skipped)} hold no articles yet)."
            ),
        )

    shared = body.model_dump(mode="json", exclude={"source_ids"})
    jobs = [
        (sid, {**shared, "source_id": str(sid)})
        for sid, _name in keep
    ]
    batch_id = await enqueue_export_batch(
        db, jobs, batch_label=await _product_label(db, product)
    )
    return ProductExportCreatedResponse(
        batch_id=batch_id, total=len(jobs), skipped=skipped
    )


async def _load_batch(db: AsyncSession, batch_id: uuid.UUID, principal: Principal):
    """Batch jobs in order, after authorizing the product they belong to."""
    rows = (
        await db.execute(
            select(ExportJob, DocumentationSource.name, DocumentationSource.product_id)
            .join(DocumentationSource, DocumentationSource.id == ExportJob.source_id)
            .where(ExportJob.batch_id == batch_id)
            .order_by(ExportJob.batch_seq)
        )
    ).all()
    if not rows:
        raise HTTPException(status_code=404, detail="Export batch not found")
    # Every job in a batch belongs to one product by construction; authorize once.
    await authorize_product(db, principal, rows[0][2], write=False)
    return rows


@router.get("/batches/{batch_id}", response_model=ExportBatchResponse)
async def get_export_batch(
    batch_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """Aggregate status of a product export, plus a row per source."""
    rows = await _load_batch(db, batch_id, principal)
    counts = {s.value: 0 for s in ExportStatus}
    sources, total_size = [], 0
    for job, source_name, _pid in rows:
        counts[job.status.value] += 1
        result = job.result or {}
        total_size += int(result.get("total_size_bytes") or 0)
        sources.append(BatchSourceStatus(
            job_id=job.id, source_id=job.source_id, source_name=source_name,
            seq=job.batch_seq or 0, status=job.status.value, export_id=job.export_id,
            article_count=result.get("total_articles"),
            size_bytes=result.get("total_size_bytes"),
            error_message=job.error_message,
        ))
    in_flight = counts["pending"] + counts["running"]
    return ExportBatchResponse(
        batch_id=batch_id,
        label=rows[0][0].batch_label or "",
        total=len(rows),
        completed=counts["completed"], failed=counts["failed"],
        pending=counts["pending"], running=counts["running"],
        cancelled=counts["cancelled"],
        finished=in_flight == 0,
        total_size_bytes=total_size,
        sources=sources,
    )


def _batch_zip_name(label: str) -> str:
    """``Vendor / Product`` -> ``Vendor_Product.zip``."""
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)
    while "__" in safe:
        safe = safe.replace("__", "_")
    return f"{safe.strip('_') or 'export'}.zip"


@router.get("/batches/{batch_id}/download")
async def download_export_batch(
    batch_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """One zip for the whole product, a folder per source.

    Each completed source contributes exactly what its own export produced. When
    that export made a zip, its entries are copied across verbatim rather than
    its loose files being walked: ``_register_image`` deliberately never stages
    images under the export directory (it streams them from the media root at zip
    time to avoid doubling the exports volume), so the loose files are the
    markdown alone. Walking them would have produced an image-less bundle
    silently, which is worse than not offering the download.

    Nesting under ``<Source>/`` keeps the markdown's relative ``images/...``
    links working, since they resolve inside the same folder.

    Cached under ``exports/batch-<batch_id>/`` and rebuilt only when missing, so
    a repeated or resumed download doesn't regenerate hundreds of MB. That
    directory has no export_jobs row, so export retention sweeps it with the
    orphan-directory policy.
    """
    rows = await _load_batch(db, batch_id, principal)
    done = [
        (job, name) for job, name, _pid in rows
        if job.status == ExportStatus.COMPLETED and job.export_id
    ]
    if not done:
        raise HTTPException(
            status_code=409, detail="No source of this batch has completed yet"
        )

    label = rows[0][0].batch_label or "export"
    zip_name = _batch_zip_name(label)
    batch_dir = os.path.join(export_engine.export_dir, f"batch-{batch_id}")
    zip_path = os.path.join(batch_dir, zip_name)

    in_flight = any(
        job.status in (ExportStatus.PENDING, ExportStatus.RUNNING)
        for job, _n, _p in rows
    )
    if os.path.isfile(zip_path):
        # A cached zip built while sources were still running would be missing
        # them, and every later download would serve that stale copy.
        if not in_flight:
            return FileResponse(
                zip_path, media_type="application/zip", filename=zip_name
            )
        os.remove(zip_path)

    os.makedirs(batch_dir, exist_ok=True)
    written = 0
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as out:
            for job, source_name in done:
                src_dir = os.path.join(export_engine.export_dir, str(job.export_id))
                if not os.path.isdir(src_dir):
                    continue  # purged by retention since the job completed
                folder = _batch_zip_name(source_name)[:-4] or str(job.export_id)
                written += _append_export(out, src_dir, folder)
    except Exception:
        # Never leave a half-written archive behind to be served as cached.
        with contextlib.suppress(OSError):
            os.remove(zip_path)
        raise

    if not written:
        with contextlib.suppress(OSError):
            os.remove(zip_path)
        raise HTTPException(
            status_code=410,
            detail="This batch's files have been purged by export retention",
        )
    return FileResponse(zip_path, media_type="application/zip", filename=zip_name)


def _append_export(out: zipfile.ZipFile, src_dir: str, folder: str) -> int:
    """Add one child export to *out* under ``folder/``; returns entries written.

    Prefers the child's own zip (the only place its images exist) and falls back
    to the loose files for a text-only or PDF export, which produces none.
    Entries are streamed one at a time so an image-heavy source doesn't have to
    fit in memory.
    """
    bundles = [f for f in sorted(os.listdir(src_dir)) if f.endswith(".zip")]
    written = 0
    if bundles:
        with zipfile.ZipFile(os.path.join(src_dir, bundles[0])) as inner:
            for info in inner.infolist():
                if info.is_dir():
                    continue
                target = zipfile.ZipInfo(f"{folder}/{info.filename}", info.date_time)
                target.compress_type = info.compress_type
                target.external_attr = info.external_attr
                with inner.open(info) as fsrc, out.open(target, "w") as fdst:
                    shutil.copyfileobj(fsrc, fdst, length=1024 * 1024)
                written += 1
        return written
    for name in sorted(os.listdir(src_dir)):
        path = os.path.join(src_dir, name)
        if os.path.isfile(path):
            out.write(path, f"{folder}/{name}")
            written += 1
    return written
