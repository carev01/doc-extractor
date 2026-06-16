"""Extraction trigger and status routes."""

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.extraction_run import ExtractionRun
from app.models.source import DocumentationSource, SourceStatus
from app.schemas.export import ExtractionTriggerResponse
from app.services.firecrawl import firecrawl_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/extraction", tags=["extraction"])


@router.post("/trigger/{source_id}", response_model=ExtractionTriggerResponse)
async def trigger_extraction(
    source_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """Trigger a full extraction for a documentation source.

    The extraction runs in the background. Poll /api/extraction/runs/{run_id}
    for status updates.
    """
    # Verify source exists and isn't already extracting
    result = await db.execute(
        select(DocumentationSource).where(DocumentationSource.id == source_id)
    )
    source = result.scalar_one_or_none()
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")

    if source.status == SourceStatus.EXTRACTING:
        raise HTTPException(
            status_code=409,
            detail="Extraction already in progress for this source",
        )

    # Create a run record synchronously
    run = ExtractionRun(
        source_id=source_id,
        status="running",
    )
    db.add(run)
    source.status = SourceStatus.EXTRACTING
    await db.commit()
    await db.refresh(run)

    # Schedule the actual extraction as a background task
    # Pass run_id so the background task updates the SAME run row
    # instead of creating a duplicate.
    background_tasks.add_task(
        _run_extraction_background, source_id, run.id
    )

    return ExtractionTriggerResponse(
        run_id=run.id,
        source_id=source_id,
        status="running",
        message="Extraction started. Poll /api/extraction/runs/{run_id} for progress.",
    )


async def _run_extraction_background(source_id: uuid.UUID, run_id: uuid.UUID):
    """Background task: execute extraction and update run status.

    Uses the pre-created run row (identified by run_id) so the
    extraction ledger stays consistent — no orphaned 'running' rows.
    """
    from app.core.database import async_session

    async with async_session() as db:
        try:
            await firecrawl_service.extract_source(db, source_id, run_id=run_id)
            await db.commit()
        except Exception as e:
            # Ensure the DB is rolled back so we don't leave partial data.
            await db.rollback()
            logger.error(
                "Extraction failed for source_id=%s run_id=%s: %s",
                source_id, run_id, e,
            )
            # The run/source status should already be FAILED inside
            # extract_source, but as a safety net, check and set it here.
            try:
                run_result = await db.execute(
                    select(ExtractionRun).where(ExtractionRun.id == run_id)
                )
                run = run_result.scalar_one_or_none()
                if run and run.status == "running":
                    run.status = "failed"
                    run.error_message = f"Background task error: {e}"[:4096]
                    run.completed_at = datetime.now(timezone.utc)

                    src_result = await db.execute(
                        select(DocumentationSource).where(
                            DocumentationSource.id == source_id
                        )
                    )
                    src = src_result.scalar_one_or_none()
                    if src and src.status == SourceStatus.EXTRACTING:
                        src.status = SourceStatus.FAILED
                        src.error_message = f"Extraction failed: {e}"[:4096]

                    await db.commit()
            except Exception as nested:
                logger.exception(
                    "Failed to update run status after error: %s", nested
                )


@router.get("/runs/{run_id}")
async def get_run_status(run_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Get the status of an extraction run."""
    result = await db.execute(
        select(ExtractionRun).where(ExtractionRun.id == run_id)
    )
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    return {
        "id": run.id,
        "source_id": run.source_id,
        "status": run.status,
        "articles_extracted": run.articles_extracted,
        "articles_total": run.articles_total,
        "error_message": run.error_message,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
    }


@router.get("/runs")
async def list_runs(
    source_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
):
    """List extraction runs, optionally filtered by source."""
    query = select(ExtractionRun).order_by(ExtractionRun.started_at.desc())

    if source_id:
        query = query.where(ExtractionRun.source_id == source_id)

    result = await db.execute(query.limit(50))
    runs = result.scalars().all()

    return {
        "runs": [
            {
                "id": r.id,
                "source_id": r.source_id,
                "status": r.status,
                "articles_extracted": r.articles_extracted,
                "articles_total": r.articles_total,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "completed_at": r.completed_at.isoformat() if r.completed_at else None,
            }
            for r in runs
        ]
    }