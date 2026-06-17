"""Export routes — markdown export and file download."""

import os
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.schemas.export import ExportRequest, ExportResponse
from app.services.exporter import export_engine

router = APIRouter(prefix="/api/export", tags=["export"])


@router.post("/markdown", response_model=ExportResponse)
async def export_markdown(
    body: ExportRequest,
    db: AsyncSession = Depends(get_db),
):
    """Generate a markdown export based on the request parameters."""
    try:
        result = await export_engine.export(
            db=db,
            source_id=body.source_id,
            article_ids=body.article_ids,
            toc_entry_ids=body.toc_entry_ids,
            topic_query=body.topic_query,
            split_by=body.split_by,
            max_articles_per_file=body.max_articles_per_file,
            max_file_size_bytes=body.max_file_size_bytes,
            max_tokens_per_file=body.max_tokens_per_file,
            respect_chapters=body.respect_chapters,
        )
        return ExportResponse(**result)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


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
                    if f.is_file() and f.name.endswith(".md")
                ]
                exports.append({
                    "export_id": str(export_uuid),
                    "file_count": len(files),
                    "files": sorted(files),
                })
            except ValueError:
                continue

    return {"exports": exports[:20]}
