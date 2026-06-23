"""Extraction trigger, status, and webhook receiver routes."""

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.article import Article
from app.models.article_version import ArticleVersion
from app.models.extraction_run import ExtractionRun, RunStatus
from app.models.product import Product
from app.models.source import DocumentationSource
from app.models.toc import TOCEntry
from app.models.toc_checkpoint import TocCheckpoint
from app.models.vendor import Vendor
from app.schemas.export import ExtractionTriggerResponse
from app.services.diffing import compute_unified_diff
from app.services.firecrawl import compute_content_hash, firecrawl_service
from app.services.queue import enqueue_run, ActiveRunExists
from app.services.sanitize import sanitize_markdown

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
        "control": run.control,
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
    status: str | None = None,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
):
    """List extraction runs (newest first) with vendor/product/source names.

    Optionally filter by ``source_id`` or ``status``. Used by the per-source run
    history and by the unified Jobs view (no source filter = all runs).
    """
    limit = max(1, min(limit, 500))
    query = (
        select(
            ExtractionRun,
            DocumentationSource.name.label("source_name"),
            Product.name.label("product_name"),
            Vendor.name.label("vendor_name"),
        )
        .join(DocumentationSource, ExtractionRun.source_id == DocumentationSource.id)
        .join(Product, DocumentationSource.product_id == Product.id)
        .join(Vendor, Product.vendor_id == Vendor.id)
        .order_by(ExtractionRun.started_at.desc())
    )
    if source_id:
        query = query.where(ExtractionRun.source_id == source_id)
    if status:
        query = query.where(ExtractionRun.status == status)

    rows = (await db.execute(query.limit(limit))).all()

    return {
        "runs": [
            {
                "id": r.id,
                "source_id": r.source_id,
                "source_name": source_name,
                "product_name": product_name,
                "vendor_name": vendor_name,
                "status": r.status,
                "control": r.control,
                "trigger": r.trigger,
                "current_phase": r.current_phase,
                "firecrawl_job_id": r.firecrawl_job_id,
                "articles_extracted": r.articles_extracted,
                "articles_total": r.articles_total,
                "articles_updated": r.articles_updated,
                "articles_unchanged": r.articles_unchanged,
                "attempts": r.attempts,
                "error_message": r.error_message,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "completed_at": r.completed_at.isoformat() if r.completed_at else None,
                "heartbeat_at": r.heartbeat_at.isoformat() if r.heartbeat_at else None,
            }
            for r, source_name, product_name, vendor_name in rows
        ]
    }


@router.get("/runs/{run_id}/logs")
async def get_run_logs(run_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Return the captured raw worker logs for a run (tail-capped at write time)."""
    log_text = (
        await db.execute(
            select(ExtractionRun.log_text).where(ExtractionRun.id == run_id)
        )
    ).scalar_one_or_none()
    if log_text is None:
        # Distinguish missing run from a run with no logs yet.
        exists = (
            await db.execute(
                select(ExtractionRun.id).where(ExtractionRun.id == run_id)
            )
        ).scalar_one_or_none()
        if exists is None:
            raise HTTPException(status_code=404, detail="Run not found")
    return {"run_id": run_id, "log_text": log_text or ""}


async def _load_run(run_id: uuid.UUID, db: AsyncSession) -> ExtractionRun:
    run = (
        await db.execute(select(ExtractionRun).where(ExtractionRun.id == run_id))
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


async def _clear_checkpoint(source_id: uuid.UUID, db: AsyncSession) -> None:
    """Drop the resume checkpoint so a future run starts fresh (used on cancel)."""
    await db.execute(delete(TocCheckpoint).where(TocCheckpoint.source_id == source_id))


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Cancel a run. Queued/paused runs end immediately (and their resume
    checkpoint is discarded); a running run gets a cooperative cancel signal that
    the worker honours at the next batch boundary."""
    run = await _load_run(run_id, db)
    from datetime import datetime, timezone

    if run.status in (RunStatus.PENDING, RunStatus.PAUSED):
        run.status = RunStatus.CANCELLED
        run.control = None
        run.completed_at = datetime.now(timezone.utc)
        await _clear_checkpoint(run.source_id, db)
        await db.commit()
        return {"id": run.id, "status": run.status.value}

    if run.status == RunStatus.RUNNING:
        run.control = "cancel"
        await db.commit()
        return {"id": run.id, "status": run.status.value, "control": "cancel"}

    raise HTTPException(
        status_code=409, detail=f"Run is {run.status.value}; nothing to cancel"
    )


@router.post("/runs/{run_id}/pause")
async def pause_run(run_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Pause a run, keeping its resume checkpoint. A running run is signalled and
    pauses at the next batch boundary; a queued run is held as PAUSED."""
    run = await _load_run(run_id, db)
    if run.status == RunStatus.RUNNING:
        run.control = "pause"
        await db.commit()
        return {"id": run.id, "status": run.status.value, "control": "pause"}
    if run.status == RunStatus.PENDING:
        run.status = RunStatus.PAUSED
        run.control = None
        await db.commit()
        return {"id": run.id, "status": run.status.value}
    raise HTTPException(
        status_code=409, detail=f"Run is {run.status.value}; cannot pause"
    )


@router.post("/runs/{run_id}/resume")
async def resume_run(run_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Resume a paused run by re-queuing it; the worker re-claims it and continues
    from the checkpoint. 409 if another run is already active for the source."""
    run = await _load_run(run_id, db)
    if run.status != RunStatus.PAUSED:
        raise HTTPException(
            status_code=409, detail=f"Run is {run.status.value}; only paused runs resume"
        )
    run.status = RunStatus.PENDING
    run.control = None
    run.claimed_by = None
    run.claimed_at = None
    run.heartbeat_at = None
    run.error_message = None
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Another run is already active for this source",
        )
    return {"id": run.id, "status": run.status.value}


@router.post("/resanitize/{source_id}")
async def resanitize_source(source_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Re-apply the current sanitizer to a source's stored articles.

    Content sanitization runs at write time, so existing articles are only
    healed when their source page actually changes. When the *sanitizer* itself
    improves, this endpoint re-cleans already-stored content without re-scraping.

    For each article whose content changes under the current rules, the previous
    content is preserved as an ``ArticleVersion`` (audit trail + reversibility,
    with a computed diff) and the article is updated in place; ``extraction_run_id``
    on the version is left NULL since no extraction run is involved. Idempotent:
    articles that are already clean are skipped, so re-running creates no spurious
    versions. Rejected with 409 while a run is active for the source, so it never
    races the writer.
    """
    source = (
        await db.execute(
            select(DocumentationSource).where(DocumentationSource.id == source_id)
        )
    ).scalar_one_or_none()
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")

    active = (
        await db.execute(
            select(ExtractionRun.id).where(
                ExtractionRun.source_id == source_id,
                ExtractionRun.status.in_(
                    [RunStatus.PENDING, RunStatus.RUNNING, RunStatus.PAUSED]
                ),
            )
        )
    ).first()
    if active is not None:
        raise HTTPException(
            status_code=409,
            detail="A run is active for this source; re-sanitize when it finishes",
        )

    articles = (
        await db.execute(select(Article).where(Article.source_id == source_id))
    ).scalars().all()

    changed = 0
    for article in articles:
        cleaned = sanitize_markdown(article.content_markdown)
        if cleaned == article.content_markdown:
            continue
        new_hash = compute_content_hash(cleaned)
        # Preserve the pre-sanitize content for audit / side-by-side comparison.
        db.add(
            ArticleVersion(
                article_id=article.id,
                extraction_run_id=None,
                content_markdown=article.content_markdown,
                content_hash=article.content_hash,
                diff_text=compute_unified_diff(
                    article.content_markdown, cleaned,
                    from_label="pre-sanitize", to_label="sanitized",
                ),
            )
        )
        article.content_markdown = cleaned
        article.content_hash = new_hash
        article.content_size_bytes = len(cleaned.encode("utf-8"))
        article.estimated_tokens = len(cleaned) // 4
        changed += 1

    await db.commit()
    total = len(articles)
    return {
        "source_id": source_id,
        "total": total,
        "changed": changed,
        "unchanged": total - changed,
    }