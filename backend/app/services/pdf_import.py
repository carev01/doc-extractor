"""PDF source import: acquire a PDF, segment it on natural boundaries, convert
each segment to markdown, and persist articles through the existing diff path."""
from __future__ import annotations

import asyncio
import collections
import hashlib
import logging
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

import fitz  # PyMuPDF
import httpx
from sqlalchemy import delete, func, select, update

from app.core.config import settings
from app.models.article import Article
from app.models.extraction_run import ExtractionRun, RunStatus
from app.models.source import DocumentationSource, SourceStatus
from app.models.toc import TOCEntry
from app.services.pdf_convert import (
    ConvertedDoc, RenderedImage, RenderedSegment, convert_pdf, split_into_segments,
)
from app.services.pdf_escalate import escalate_segment, score_segment
from app.services.sanitize import sanitize_markdown
from app.services.versioning import derive_pdf_topic_key

logger = logging.getLogger(__name__)


class PdfAcquireError(Exception):
    """Raised when a PDF source's bytes cannot be obtained."""


def pdf_is_upload(source) -> bool:
    return str(source.base_url).startswith("file://")


def pdf_path_for(source_id, pdf_dir: str) -> str:
    return os.path.join(pdf_dir, f"{source_id}.pdf")


async def _fetch_url_bytes(url: str) -> bytes:
    timeout = httpx.Timeout(300.0, connect=30.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.content


async def acquire_pdf(source) -> tuple[bytes, str]:
    """Return (pdf_bytes, sha256_hex) for a pdf source (upload or URL origin)."""
    try:
        if pdf_is_upload(source):
            path = pdf_path_for(source.id, settings.pdf_dir)
            with open(path, "rb") as fh:
                data = fh.read()
        else:
            data = await _fetch_url_bytes(source.base_url)
    except (OSError, httpx.HTTPError) as exc:
        raise PdfAcquireError(f"Could not acquire PDF: {exc}") from exc
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


async def build_segments(
    pdf_bytes: bytes,
    progress: "collections.abc.Callable[[int, int], collections.abc.Awaitable[None]] | None" = None,
) -> list[RenderedSegment]:
    """Convert the whole PDF via docling-serve, split on heading boundaries, then
    VLM-escalate only low-confidence segments within the per-run page budget."""
    converted: ConvertedDoc = await convert_pdf(pdf_bytes)
    outline = _outline_for(pdf_bytes)
    segments = split_into_segments(converted, outline)

    if not settings.pdf_vlm_escalation_enabled:
        return segments

    flagged = [s for s in segments if score_segment(s, converted)]
    budget = settings.pdf_vlm_max_pages_per_run
    done, total = 0, len(flagged)
    for seg in flagged:
        pages = seg.page_end - seg.page_start + 1
        if pages > budget:
            continue
        new_md = await escalate_segment(pdf_bytes, seg)
        seg.markdown = new_md
        matched = [img for img in converted.images if img.filename in new_md]
        seg.images = matched or seg.images
        budget -= pages
        done += 1
        if progress is not None:
            await progress(done, total)
    return segments


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


async def run_pdf_extraction(service, db, source, run, run_pk) -> ExtractionRun:
    """Extract a PDF source into Article rows, reusing the web path's diff/version
    machinery. `service` is a FirecrawlService (for process_article_result /
    _reconcile_removals)."""
    run.current_phase = "pdf_acquire"
    source.status = SourceStatus.EXTRACTING
    await db.commit()

    pdf_bytes, pdf_hash = await acquire_pdf(source)

    # Fast path: byte-identical to the last completed run → mark all unchanged.
    prior = await _latest_completed_hash(db, source.id)
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

    run.current_phase = "pdf_convert"
    await db.commit()

    async def _convert_progress(done: int, total: int) -> None:
        run.articles_extracted = done
        await db.commit()
        if total and (done == 1 or done % 5 == 0 or done == total):
            logger.info("PDF VLM escalation: %d/%d segments re-converted", done, total)

    rendered_segments = await build_segments(pdf_bytes, _convert_progress)
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

    for toc_id, sort_order, title, topic_key, url, md, images in article_inputs:
        await service.process_article_result(
            db, source.id, run_pk, url=url, markdown_content=md, doc_html="",
            toc_entry_id=toc_id, sort_order=sort_order, title=title,
            change_status=None, topic_key=topic_key, pdf_images=images,
        )

    run = (await db.execute(
        select(ExtractionRun).where(ExtractionRun.id == run_pk)
    )).scalar_one()
    await service._reconcile_removals(db, source.id, run_pk)

    run.status = RunStatus.COMPLETED
    run.completed_at = datetime.now(timezone.utc)
    run.pdf_hash = pdf_hash
    source.status = SourceStatus.COMPLETED
    source.last_extracted_at = run.completed_at
    await db.flush()
    return run
