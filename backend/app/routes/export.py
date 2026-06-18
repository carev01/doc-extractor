"""Export routes — async export enqueue, job status, and file download."""

import os
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.export_job import ExportJob, ExportStatus
from app.models.source import DocumentationSource
from app.schemas.export import (
    ExportJobCreatedResponse,
    ExportJobStatusResponse,
    ExportRequest,
)
from app.services.exporter import export_engine
from app.services.queue import enqueue_export

router = APIRouter(prefix="/api/export", tags=["export"])


@router.post("", response_model=ExportJobCreatedResponse)
async def create_export(body: ExportRequest, db: AsyncSession = Depends(get_db)):
    """Enqueue an export job; the worker generates it. Poll /api/export/jobs/{id}."""
    src = await db.execute(
        select(DocumentationSource.id).where(DocumentationSource.id == body.source_id)
    )
    if src.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Source not found")
    job = await enqueue_export(db, body.source_id, body.model_dump(mode="json"))
    return ExportJobCreatedResponse(export_job_id=job.id, status="pending")


@router.get("/jobs/{job_id}", response_model=ExportJobStatusResponse)
async def get_export_job(job_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    job = (await db.execute(select(ExportJob).where(ExportJob.id == job_id))).scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="Export job not found")
    result = job.result or {}
    return ExportJobStatusResponse(
        id=job.id, source_id=job.source_id, status=job.status.value,
        export_id=job.export_id, zip_filename=result.get("zip_filename"),
        files=result.get("files"), error_message=job.error_message,
    )


@router.get("/download/{export_id}")
async def download_export_zip(export_id: uuid.UUID):
    """Download the self-contained zip bundle (markdown + images) for an export."""
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
async def download_export_file(export_id: uuid.UUID, filename: str):
    """Download a specific export file."""
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

    return FileResponse(
        real_path,
        media_type="text/markdown",
        filename=filename,
    )


@router.get("/list")
async def list_exports(db: AsyncSession = Depends(get_db)):
    """List recent exports (metadata only — files are on disk)."""
    export_dir = export_engine.export_dir
    if not os.path.isdir(export_dir):
        return {"exports": []}

    exports = []
    for entry in sorted(
        os.scandir(export_dir), key=lambda e: e.name, reverse=True
    ):
        if entry.is_dir():
            try:
                export_uuid = uuid.UUID(entry.name)
                files = [
                    f.name
                    for f in os.scandir(entry.path)
                    if f.is_file() and f.name.endswith((".md", ".pdf"))
                ]
                exports.append({
                    "export_id": str(export_uuid),
                    "file_count": len(files),
                    "files": sorted(files),
                })
            except ValueError:
                continue

    return {"exports": exports[:20]}
