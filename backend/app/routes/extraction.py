"""Extraction trigger, status, and webhook receiver routes."""

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.extraction_run import ExtractionRun
from app.models.source import DocumentationSource
from app.models.toc import TOCEntry
from app.schemas.export import ExtractionTriggerResponse
from app.services.firecrawl import firecrawl_service
from app.services.queue import enqueue_run, ActiveRunExists

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/extraction", tags=["extraction"])


@router.post("/trigger/{source_id}", response_model=ExtractionTriggerResponse)
async def trigger_extraction(
    source_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Queue a full extraction for a source. A worker picks it up.

    Poll /api/extraction/runs/{run_id} for status (pending -> running -> completed).
    """
    result = await db.execute(
        select(DocumentationSource).where(DocumentationSource.id == source_id)
    )
    source = result.scalar_one_or_none()
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")

    try:
        run = await enqueue_run(db, source_id, trigger="manual")
    except ActiveRunExists:
        raise HTTPException(
            status_code=409,
            detail="Extraction already queued or running for this source",
        )

    return ExtractionTriggerResponse(
        run_id=run.id,
        source_id=source_id,
        status="pending",
        message="Extraction queued. Poll /api/extraction/runs/{run_id} for progress.",
    )


@router.post("/webhook/{run_id}", include_in_schema=False)
async def firecrawl_webhook(
    run_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Receive Firecrawl batch_scrape.page webhook events.

    Firecrawl calls this once per completed page when a batch job was submitted
    with a webhook URL. We process the article inline and return 200 immediately
    so Firecrawl doesn't retry. The background task polls only for completion.
    """
    payload: dict[str, Any] = await request.json()
    event_type = payload.get("type", "")

    if event_type != "batch_scrape.page":
        # Ignore started / completed / failed events — background task handles lifecycle.
        return {"ok": True}

    data_items = payload.get("data", [])
    if not data_items:
        return {"ok": True}

    page = data_items[0]
    meta = page.get("metadata", {})
    url = meta.get("sourceURL") or meta.get("url", "")
    markdown = page.get("markdown", "")
    html = page.get("html", "")

    if not url:
        logger.warning("Webhook event missing URL for run %s", run_id)
        return {"ok": True}

    # Look up the run to get source_id
    run_result = await db.execute(
        select(ExtractionRun).where(ExtractionRun.id == run_id)
    )
    run = run_result.scalar_one_or_none()
    if not run:
        logger.warning("Webhook event for unknown run %s", run_id)
        return {"ok": True}

    source_id = run.source_id

    # Look up TOC metadata for this URL
    toc_result = await db.execute(
        select(TOCEntry).where(
            TOCEntry.source_id == source_id,
            TOCEntry.url == url,
        )
    )
    toc_entry = toc_result.scalar_one_or_none()
    toc_entry_id = toc_entry.id if toc_entry else None
    sort_order = toc_entry.sort_order if toc_entry else 0
    title = toc_entry.title if toc_entry else url

    # Retry if Firecrawl returned empty content
    if not markdown.strip():
        logger.warning("Empty content for %s via webhook — retrying individually", url)
        for attempt in range(firecrawl_service.EMPTY_CONTENT_RETRIES):
            import asyncio
            await asyncio.sleep(firecrawl_service.EMPTY_CONTENT_RETRY_DELAY)
            markdown, html, _cs, _dt = await firecrawl_service._scrape_article(
                url,
                content_config=firecrawl_service._content_config_by_source.get(source_id),
            )
            if markdown.strip():
                break
            logger.warning(
                "Still empty for %s (retry %d/%d)",
                url, attempt + 1, firecrawl_service.EMPTY_CONTENT_RETRIES,
            )

    outcome = await firecrawl_service.process_article_result(
        db=db,
        source_id=source_id,
        run_id=run_id,
        url=url,
        markdown_content=markdown,
        doc_html=html,
        toc_entry_id=toc_entry_id,
        sort_order=sort_order,
        title=title,
    )
    logger.info("Webhook processed %s: %s", url, outcome)
    return {"ok": True}


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
        "trigger": run.trigger,
        "current_phase": run.current_phase,
        "firecrawl_job_id": run.firecrawl_job_id,
        "articles_extracted": run.articles_extracted,
        "articles_total": run.articles_total,
        "articles_updated": run.articles_updated,
        "articles_unchanged": run.articles_unchanged,
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
                "trigger": r.trigger,
                "current_phase": r.current_phase,
                "firecrawl_job_id": r.firecrawl_job_id,
                "articles_extracted": r.articles_extracted,
                "articles_total": r.articles_total,
                "articles_updated": r.articles_updated,
                "articles_unchanged": r.articles_unchanged,
                "error_message": r.error_message,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "completed_at": r.completed_at.isoformat() if r.completed_at else None,
            }
            for r in runs
        ]
    }