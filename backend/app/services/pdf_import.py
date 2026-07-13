"""PDF source import: acquire a PDF, segment it on natural boundaries, convert
each segment to markdown, and persist articles through the existing diff path."""
from __future__ import annotations

import asyncio
import collections
import hashlib
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

import fitz  # PyMuPDF
import httpx
from sqlalchemy import delete, func, select, update

from app.core.config import settings
from app.models.article import Article
from app.models.article_version import ArticleVersion
from app.models.extraction_run import ExtractionRun, RunStatus
from app.models.source import DocumentationSource, SourceStatus
from app.models.toc import TOCEntry
from app.services.pdf_convert import (
    ConvertedDoc, RenderedImage, RenderedSegment, convert_pdf, split_into_segments,
)
from app.services import change_log
from app.services.pdf_escalate import escalate_segment, escalate_segments
from app.services.sanitize import sanitize_markdown
from app.services.versioning import derive_pdf_topic_key

logger = logging.getLogger(__name__)


class PdfAcquireError(Exception):
    """Raised when a PDF source's bytes cannot be obtained."""


def pdf_is_upload(source) -> bool:
    return str(source.base_url).startswith("file://")


def pdf_path_for(source_id, pdf_dir: str) -> str:
    return os.path.join(pdf_dir, f"{source_id}.pdf")


_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


async def _fetch_url_bytes(url: str, cookies: list[dict] | None = None) -> bytes:
    """GET the PDF bytes. ``cookies`` (a realm cookie list) are sent as a Cookie
    header so login-walled PDFs (e.g. a vendor docs PDF) download authenticated."""
    headers = {"User-Agent": _BROWSER_UA}
    if cookies:
        pairs = [f"{c['name']}={c['value']}" for c in cookies if c.get("name")]
        if pairs:
            headers["Cookie"] = "; ".join(pairs)
    timeout = httpx.Timeout(300.0, connect=30.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.content


def _looks_like_pdf(data: bytes | None) -> bool:
    return bool(data) and data[:5] == b"%PDF-"


async def _fetch_url_bytes_via_browser(url: str, cookies: list[dict] | None) -> bytes:
    """Fallback download in real Chrome (browser TLS + cookies) for hosts whose
    CDN bot-shield serves a login/HTML shell to plain HTTP clients."""
    from app.services.browserless import browserless_client

    auth_state = {"cookies": cookies} if cookies else None
    return await browserless_client.download_bytes(url, auth_state=auth_state)


async def acquire_pdf(source, auth_cookies: list[dict] | None = None) -> tuple[bytes, str]:
    """Return (pdf_bytes, sha256_hex) for a pdf source (upload or URL origin).
    ``auth_cookies`` authenticate a login-walled PDF URL. For URL sources whose
    CDN serves a non-PDF shell to the plain HTTP client (bot fingerprinting), fall
    back to downloading the file in real Chrome via Browserless."""
    if pdf_is_upload(source):
        try:
            path = pdf_path_for(source.id, settings.pdf_dir)
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            raise PdfAcquireError(f"Could not read uploaded PDF: {exc}") from exc
    else:
        data = None
        try:
            data = await _fetch_url_bytes(source.base_url, cookies=auth_cookies)
        except (OSError, httpx.HTTPError) as exc:
            logger.info("PDF direct download failed (%s); retrying via Browserless", exc)
        if not _looks_like_pdf(data):
            logger.info(
                "PDF URL %s did not return a PDF via HTTP; retrying via Browserless",
                source.base_url,
            )
            try:
                data = await _fetch_url_bytes_via_browser(source.base_url, auth_cookies)
            except Exception as exc:
                raise PdfAcquireError(
                    f"Could not acquire PDF (HTTP + Browserless): {exc}"
                ) from exc

    if not data:
        raise PdfAcquireError("PDF is empty")
    return data, hashlib.sha256(data).hexdigest()


@dataclass
class Segment:
    title: str
    level: int
    page_start: int          # 0-based, inclusive
    page_end: int            # 0-based, inclusive
    path: list[str] = field(default_factory=list)


def _outline_segments(doc: "fitz.Document") -> list[Segment]:
    toc = doc.get_toc(simple=True)  # [[level, title, page1based], ...]
    if not toc:
        return []
    last_page = doc.page_count - 1
    segs: list[Segment] = []
    stack: list[str] = []  # ancestor titles by level
    for i, (level, title, page1) in enumerate(toc):
        start = max(0, page1 - 1)
        # End = page before the very next TOC entry (any level).
        end = last_page
        if i + 1 < len(toc):
            nxt_page1 = toc[i + 1][2]
            end = max(start, nxt_page1 - 2)
        stack = stack[: level - 1]
        stack.append(title)
        segs.append(Segment(
            title=title, level=level, page_start=start, page_end=end,
            path=list(stack),
        ))
    return segs


def _outline_for(pdf_bytes: bytes) -> list[Segment]:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        return _outline_segments(doc)
    finally:
        doc.close()


async def process_segments(pdf_bytes, outline, on_poll=None):
    """Convert (off-loop, async), split on headings, then VLM-escalate the
    exclusive low-confidence segments. Returns (segments, converted)."""
    converted = await convert_pdf(pdf_bytes, on_poll=on_poll)
    segments = split_into_segments(converted, outline)
    await escalate_segments(pdf_bytes, segments, converted)
    return segments, converted


async def _latest_completed_hash(db, source_id) -> str | None:
    return (
        await db.execute(
            select(ExtractionRun.pdf_hash)
            .where(
                ExtractionRun.source_id == source_id,
                ExtractionRun.status == RunStatus.COMPLETED,
                ExtractionRun.pdf_hash.isnot(None),
            )
            .order_by(ExtractionRun.started_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def run_pdf_extraction(service, db, source, run, run_pk,
                             auth_state: dict | None = None) -> ExtractionRun:
    """Extract a PDF source into Article rows, reusing the web path's diff/version
    machinery. `service` is a FirecrawlService (for process_article_result /
    _reconcile_removals). ``auth_state`` (resolved by extract_source) authenticates
    a login-walled PDF URL."""
    run.current_phase = "pdf_acquire"
    source.status = SourceStatus.EXTRACTING
    await db.commit()

    pdf_bytes, pdf_hash = await acquire_pdf(source, auth_cookies=(auth_state or {}).get("cookies"))

    # Fast path: byte-identical to the last completed run → mark all unchanged.
    # Skipped for a forced run (trigger="force"), so a conversion/segmentation
    # fix re-applies to a PDF whose bytes haven't changed.
    prior = None if run.trigger == "force" else await _latest_completed_hash(db, source.id)
    existing_count = (
        await db.execute(
            select(func.count()).select_from(Article).where(
                Article.source_id == source.id, Article.removed_at.is_(None)
            )
        )
    ).scalar()
    now = datetime.now(timezone.utc)
    if prior == pdf_hash and existing_count:
        await db.execute(
            update(Article)
            .where(Article.source_id == source.id, Article.removed_at.is_(None))
            .values(extracted_at=now)
        )
        run = (await db.execute(
            select(ExtractionRun).where(ExtractionRun.id == run_pk)
        )).scalar_one()
        run.status = RunStatus.COMPLETED
        run.completed_at = now
        run.pdf_hash = pdf_hash
        run.articles_total = existing_count
        run.articles_unchanged = existing_count
        source.status = SourceStatus.COMPLETED
        source.last_extracted_at = now
        await db.flush()
        return run

    outline = await asyncio.to_thread(_outline_for, pdf_bytes)
    run.articles_total = len(outline)
    run.current_phase = "pdf_convert"
    await db.commit()
    logger.info("pdf_convert: converting %d-outline-entry PDF via docling-serve", len(outline))

    _t0 = time.monotonic()
    async def _on_poll(status: dict) -> None:
        logger.info("pdf_convert: still processing (status=%s, queue=%s, %.0fs elapsed)",
                    status.get("task_status"), status.get("task_position"),
                    time.monotonic() - _t0)

    async def _on_convert_progress(pages_done: int, pages_total: int) -> None:
        # Drive a determinate progress bar during a batched conversion: report
        # pages converted so far against the page total. The frontend renders
        # this (phase pdf_convert + articles_extracted > 0) as a real bar; a
        # single-shot conversion never calls this, so it stays indeterminate.
        run.articles_extracted = pages_done
        run.articles_total = pages_total
        await db.commit()
        # Honour a pending cancel/pause at batch boundaries so a multi-hour
        # conversion can be stopped without waiting for the whole document.
        await service._raise_if_controlled(db, run_pk)

    await service._raise_if_controlled(db, run_pk)
    converted = await convert_pdf(
        pdf_bytes, on_poll=_on_poll, on_progress=_on_convert_progress)
    run.current_phase = "pdf_split"
    await db.commit()
    await service._raise_if_controlled(db, run_pk)
    # split_into_segments is CPU-heavy on a large document; run it off the event
    # loop so the worker heartbeat keeps ticking (otherwise the run is reaped as
    # "worker lost").
    rendered_segments = await asyncio.to_thread(split_into_segments, converted, outline)
    logger.info("pdf_split: %d article segments (%s engine)",
                len(rendered_segments), converted.engine)

    run.current_phase = "pdf_escalate"
    await db.commit()
    escalation_failed_idx = await escalate_segments(pdf_bytes, rendered_segments, converted)
    run.articles_total = len(rendered_segments)
    await db.commit()

    await db.execute(delete(TOCEntry).where(TOCEntry.source_id == source.id))
    await db.flush()

    entry_ids: list[uuid.UUID] = []
    levels: list[int] = []
    article_inputs: list[tuple] = []
    key_counts: dict[str, int] = {}
    for i, seg in enumerate(rendered_segments):
        parent_id = None
        for j in range(i - 1, -1, -1):
            if levels[j] < seg.level:
                parent_id = entry_ids[j]
                break
        base_key = derive_pdf_topic_key(seg.path or [seg.title])
        n = key_counts.get(base_key, 0) + 1
        key_counts[base_key] = n
        topic_key = base_key if n == 1 else f"{base_key}-{n}"
        page_anchor = f"#page={seg.page_start + 1}"
        url = f"{source.base_url}{page_anchor}"
        toc = TOCEntry(
            source_id=source.id, title=seg.title, url=url,
            level=seg.level, sort_order=i, is_article=True, parent_id=parent_id,
        )
        db.add(toc)
        await db.flush()
        entry_ids.append(toc.id)
        levels.append(seg.level)
        article_inputs.append(
            (toc.id, i, seg.title, topic_key, url, seg.markdown, seg.images)
        )

    run.current_phase = "content_scraping"
    # The convert phase used articles_extracted to report conversion progress;
    # reset it so process_article_result counts persisted articles from zero.
    run.articles_extracted = 0
    # A segment that renders to empty markdown (e.g. an image-only page) is not
    # persisted by process_article_result, so it must not count toward the total —
    # otherwise progress can never reach 100%.
    run.articles_total = sum(1 for inp in article_inputs if inp[5].strip())
    await db.commit()

    for idx, (toc_id, sort_order, title, topic_key, url, md, images) in enumerate(article_inputs):
        if idx % 10 == 0:
            await service._raise_if_controlled(db, run_pk)
        await service.process_article_result(
            db, source.id, run_pk, url=url, markdown_content=md, doc_html="",
            toc_entry_id=toc_id, sort_order=sort_order, title=title,
            change_status=None, topic_key=topic_key, pdf_images=images,
        )

    run = (await db.execute(
        select(ExtractionRun).where(ExtractionRun.id == run_pk)
    )).scalar_one()
    await service._reconcile_removals(db, source.id, run_pk)

    # Record any segments whose VLM escalation failed so the run completes with a
    # warning (not a clean green) and can be retried without redoing the (often
    # >1h) Layer-A conversion. Map each failed segment back to its persisted
    # article by the topic_key assigned above.
    run.escalation_pending = await _build_escalation_pending(
        db, source.id, rendered_segments, article_inputs, escalation_failed_idx
    ) or None

    run.status = RunStatus.COMPLETED
    run.completed_at = datetime.now(timezone.utc)
    run.pdf_hash = pdf_hash
    source.status = SourceStatus.COMPLETED
    source.last_extracted_at = run.completed_at
    await db.flush()
    return run


async def _build_escalation_pending(
    db, source_id, rendered_segments, article_inputs, failed_idx,
) -> list[dict]:
    """Map failed-escalation segment indices to {article_id, page range, …} dicts
    for ExtractionRun.escalation_pending (JSON-serialisable)."""
    if not failed_idx:
        return []
    topic_keys = {article_inputs[i][3] for i in failed_idx if i < len(article_inputs)}
    rows = await db.execute(
        select(Article.id, Article.topic_key).where(
            Article.source_id == source_id, Article.topic_key.in_(topic_keys)
        )
    )
    id_by_key = {tk: aid for aid, tk in rows}
    pending: list[dict] = []
    for i in failed_idx:
        if i >= len(article_inputs):
            continue
        topic_key = article_inputs[i][3]
        article_id = id_by_key.get(topic_key)
        if article_id is None:
            continue  # empty-markdown segment was not persisted
        seg = rendered_segments[i]
        pending.append({
            "article_id": str(article_id),
            "page_start": seg.page_start,
            "page_end": seg.page_end,
            "level": seg.level,
            "title": seg.title,
        })
    return pending


async def retry_escalation(service, db, source, run, run_pk,
                           auth_state: dict | None = None) -> ExtractionRun:
    """Re-attempt VLM escalation for the segments that failed on a prior run,
    WITHOUT redoing the Layer-A conversion. ``run.escalation_pending`` (copied
    from the failed run when this retry was enqueued) lists the page ranges to
    re-convert; we re-acquire the PDF and re-escalate just those, updating the
    affected articles in place and snapshotting their prior content as a version.

    Leaves still-failing segments in ``escalation_pending`` (so the run keeps its
    warning and can be retried again); clears it to NULL when all succeed."""
    from app.services.firecrawl import compute_content_hash

    pending: list[dict] = list(run.escalation_pending or [])
    run.current_phase = "pdf_escalate"
    run.articles_total = len(pending)
    run.articles_extracted = 0
    run.articles_updated = 0
    source.status = SourceStatus.EXTRACTING
    # Commit a run_start sentinel before mutating any article, so this escalate
    # run has a committed floor in content_changes.id space (parity with
    # extract_source, which this path bypasses). The delta feed's safe-ceiling
    # then withholds this run's mid-run rows until it completes. See
    # services/delta_feed.py.
    await change_log.record_run_start(db, source_id=source.id, run_id=run_pk)
    await db.commit()

    if not pending:
        run.status = RunStatus.COMPLETED
        run.completed_at = datetime.now(timezone.utc)
        run.escalation_pending = None
        source.status = SourceStatus.COMPLETED
        await db.flush()
        return run

    pdf_bytes, _ = await acquire_pdf(source, auth_cookies=(auth_state or {}).get("cookies"))

    still_failing: list[dict] = []
    updated = 0
    consecutive_failures = 0
    now = datetime.now(timezone.utc)
    for pos, entry in enumerate(pending):
        await service._raise_if_controlled(db, run_pk)
        article = await db.get(Article, uuid.UUID(entry["article_id"]))
        if article is None:
            continue  # article deleted since the original run — drop from pending
        seg = RenderedSegment(
            title=entry["title"], level=entry["level"], path=[entry["title"]],
            page_start=entry["page_start"], page_end=entry["page_end"],
            markdown=article.content_markdown, images=[],
        )
        new_md = await escalate_segment(pdf_bytes, seg)
        if new_md is None:
            consecutive_failures += 1
            still_failing.append(entry)
            if consecutive_failures >= settings.pdf_vlm_max_consecutive_failures:
                logger.warning(
                    "retry_escalation: %d consecutive VLM failures — leaving the "
                    "remaining %d segments pending", consecutive_failures,
                    len(pending) - pos - 1,
                )
                still_failing.extend(pending[pos + 1:])
                break
            continue
        consecutive_failures = 0
        if new_md != article.content_markdown:
            db.add(ArticleVersion(
                article_id=article.id, extraction_run_id=run_pk,
                content_markdown=article.content_markdown,
                content_hash=article.content_hash,
                source_url=article.source_url,
            ))
            article.content_markdown = new_md
            article.content_hash = compute_content_hash(new_md)
            article.content_size_bytes = len(new_md.encode("utf-8"))
            article.estimated_tokens = len(new_md) // 4
            article.extraction_run_id = run_pk
            article.extracted_at = now
            # Outbox: surface the VLM-improved content in the delta feed, same as
            # the main extraction path. Committed with the run at completion.
            await change_log.record_change(
                db, article=article, change_type="updated", run_id=run_pk
            )
            updated += 1
        logger.info("retry_escalation: %d/%d re-converted (%r)",
                    pos + 1, len(pending), entry["title"])

    run = (await db.execute(
        select(ExtractionRun).where(ExtractionRun.id == run_pk)
    )).scalar_one()
    run.articles_updated = updated
    run.articles_extracted = updated
    run.escalation_pending = still_failing or None
    run.status = RunStatus.COMPLETED
    run.completed_at = datetime.now(timezone.utc)
    source.status = SourceStatus.COMPLETED
    source.last_extracted_at = run.completed_at
    await db.flush()
    return run
