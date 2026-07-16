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
from app.models.extraction_run import ExtractionRun, RunStatus
from app.models.source import DocumentationSource, SourceStatus
from app.models.toc import TOCEntry
from app.services.pdf_convert import (
    RenderedSegment, _page_texts, convert_pdf, split_into_segments,
)
from app.services import change_log, pdf_cache
from app.services.pdf_escalate import escalate_low_confidence_pages, pages_in_ranges
from app.services.sanitize import sanitize_markdown
from app.services.versioning import derive_pdf_topic_key

logger = logging.getLogger(__name__)


class PdfAcquireError(Exception):
    """Raised when a PDF source's bytes cannot be obtained.

    ``retryable`` marks a transient failure (a URL download / Browserless fetch
    that may succeed on a later attempt — e.g. Dell's intermittently-failing CDN)
    versus a permanent one (a missing uploaded file). The worker requeues
    retryable failures with backoff instead of failing the run outright.
    """

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


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
            # A missing/unreadable uploaded file won't fix itself — don't retry.
            raise PdfAcquireError(
                f"Could not read uploaded PDF: {exc}", retryable=False
            ) from exc
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
    """Convert (off-loop, async), VLM-escalate low-confidence PAGES, then split.
    Escalation runs BEFORE the split so the improved content feeds segmentation
    (a section the standard pipeline rendered empty can now be detected and get its
    own article). Returns (segments, converted)."""
    converted = await convert_pdf(pdf_bytes, on_poll=on_poll)
    await escalate_low_confidence_pages(pdf_bytes, converted)
    segments = split_into_segments(converted, outline)
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

    # VLM-escalate low-confidence PAGES BEFORE splitting, so the split sees the
    # improved markdown. A page (not a whole outline section) is the escalation
    # unit, so one bad table no longer drags a 100+ page chapter through the VLM;
    # and a chapter the standard pipeline rendered near-empty can, once its pages
    # are recovered, be split into its real sub-sections' articles.
    run.current_phase = "pdf_escalate"
    run.articles_extracted = 0
    await db.commit()

    async def _on_escalate(done: int, total: int, _page0: int) -> None:
        run.articles_extracted = done
        run.articles_total = total
        await db.commit()
        await service._raise_if_controlled(db, run_pk)

    failed_ranges = await escalate_low_confidence_pages(
        pdf_bytes, converted, on_page=_on_escalate)

    run.current_phase = "pdf_split"
    await db.commit()
    await service._raise_if_controlled(db, run_pk)
    # split_into_segments is CPU-heavy on a large document; run it off the event
    # loop so the worker heartbeat keeps ticking (otherwise the run is reaped as
    # "worker lost").
    rendered_segments = await asyncio.to_thread(split_into_segments, converted, outline)
    logger.info("pdf_split: %d article segments (%s engine)",
                len(rendered_segments), converted.engine)

    return await _persist_segments(
        service, db, source, run, run_pk, converted, outline,
        rendered_segments, pdf_hash, failed_ranges,
    )


async def _persist_segments(
    service, db, source, run, run_pk, converted, outline,
    rendered_segments, pdf_hash, failed_ranges,
) -> ExtractionRun:
    """Rebuild the TOC, persist articles (through the diff/version machinery),
    reconcile removals, enrich images, and finalize the run. Shared by the main
    extraction and the escalate-retry so both re-split identically. ``failed_ranges``
    (page ranges still pending escalation) is recorded on the run and, when
    non-empty, the converted doc is cached for a fast retry."""
    run.articles_total = len(rendered_segments)
    await db.commit()

    await db.execute(delete(TOCEntry).where(TOCEntry.source_id == source.id))
    await db.flush()

    entry_ids: list[uuid.UUID] = []
    levels: list[int] = []
    article_inputs: list[tuple] = []
    key_counts: dict[str, int] = {}

    def _topic_key(path: list[str] | None, title: str) -> str:
        base_key = derive_pdf_topic_key(path or [title])
        n = key_counts.get(base_key, 0) + 1
        key_counts[base_key] = n
        return base_key if n == 1 else f"{base_key}-{n}"

    def _parent_of(level: int) -> "uuid.UUID | None":
        for j in range(len(levels) - 1, -1, -1):
            if levels[j] < level:
                return entry_ids[j]
        return None

    # The TOC mirrors the PDF's own outline verbatim (every bookmark entry, full
    # hierarchy), while ARTICLES are the coarser gap-free content units keyed to
    # "detected section" outline entries (RenderedSegment.outline_index). A TOC
    # entry that doesn't start an article links into the article covering its page
    # — resolved when the TOC is served (get_toc), so nothing is dropped and every
    # entry navigates somewhere. Fall back to one-entry-per-segment only when there
    # is no usable outline (e.g. the docling-headings / whole-document paths).
    seg_by_outline: dict[int, RenderedSegment] = {
        seg.outline_index: seg for seg in rendered_segments if seg.outline_index >= 0
    }
    # An article-start's URL must be UNIQUE, not just "#page=N": several outline
    # sections routinely start on the same page (Data server / MCS / EMT all begin
    # on page 25). source_url is an article identity key for both the URL-healing
    # fallback in process_article_result and _reconcile_removals — a shared #page
    # anchor makes same-page sections collapse into one article (each is processed
    # in turn, sees the one running survivor at that URL, and overwrites it). The
    # "#page=N" fragment still deep-links the PDF page; "&s=<idx>" disambiguates the
    # section (viewers ignore it). Non-article TOC entries keep the plain page
    # anchor — they carry no article and link via the covering article at serve time.
    def _page_url(page_start: int) -> str:
        return f"{source.base_url}#page={page_start + 1}"

    if outline and seg_by_outline:
        for idx, o in enumerate(outline):
            parent_id = _parent_of(o.level)
            seg = seg_by_outline.get(idx)
            url = f"{_page_url(o.page_start)}&s={idx}" if seg is not None else _page_url(o.page_start)
            toc = TOCEntry(
                source_id=source.id, title=o.title, url=url,
                level=o.level, sort_order=idx,
                is_article=seg is not None, parent_id=parent_id,
            )
            db.add(toc)
            await db.flush()
            entry_ids.append(toc.id)
            levels.append(o.level)
            if seg is not None:
                topic_key = _topic_key(seg.path, seg.title)
                article_inputs.append(
                    (toc.id, idx, seg.title, topic_key, url, seg.markdown, seg.images)
                )
    else:
        for i, seg in enumerate(rendered_segments):
            parent_id = _parent_of(seg.level)
            topic_key = _topic_key(seg.path, seg.title)
            url = f"{_page_url(seg.page_start)}&s={i}"
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
            # PDF content is one trusted downloaded file — no per-page WAF to
            # detect; block detection here only false-flags real sections.
            detect_blocks=False,
        )

    run = (await db.execute(
        select(ExtractionRun).where(ExtractionRun.id == run_pk)
    )).scalar_one()
    await service._reconcile_removals(db, source.id, run_pk)

    # Image-enrichment phase (opt-in, best-effort): describe meaningful images and
    # inject captions. The web path runs this at the end of extract_source, but a
    # PDF source returns via run_pdf_extraction before reaching it — so without
    # this, PDF images were never auto-enriched (only via the manual action).
    run.current_phase = "image_enrich"
    await db.commit()
    from app.services import image_describe
    await image_describe.enrich_run_images(db, source.id, run_pk)

    run = (await db.execute(
        select(ExtractionRun).where(ExtractionRun.id == run_pk)
    )).scalar_one()

    # Pages that still need VLM escalation (a service outage, or over the per-run
    # budget) are recorded as page ranges so the run completes with a warning (not
    # a clean green) and can be retried. When present, the converted doc is cached
    # so the retry re-escalates just those pages and re-splits without redoing the
    # (often >1h) Layer-A conversion.
    run.escalation_pending = failed_ranges or None

    run.status = RunStatus.COMPLETED
    run.completed_at = datetime.now(timezone.utc)
    run.pdf_hash = pdf_hash
    source.status = SourceStatus.COMPLETED
    source.last_extracted_at = run.completed_at
    await db.flush()

    if failed_ranges:
        pdf_cache.save(pdf_hash, converted)
    else:
        pdf_cache.delete(pdf_hash)
    return run


async def retry_escalation(service, db, source, run, run_pk,
                           auth_state: dict | None = None) -> ExtractionRun:
    """Re-attempt VLM escalation for the PAGES that failed on a prior run, reusing
    the cached converted doc (no Layer-A re-conversion), then re-split and
    re-persist — so recovered content can form new sub-articles (the reason a
    near-empty chapter is finally split into its real sections).

    ``run.escalation_pending`` (copied from the failed run at enqueue) lists the
    page ranges to retry. Still-failing pages are left in ``escalation_pending``;
    it clears to NULL when all succeed. Falls back to a full re-conversion when no
    cache entry exists (e.g. a run from before caching)."""
    pending_ranges: list[dict] = list(run.escalation_pending or [])
    run.current_phase = "pdf_escalate"
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

    if not pending_ranges:
        run.status = RunStatus.COMPLETED
        run.completed_at = datetime.now(timezone.utc)
        run.escalation_pending = None
        source.status = SourceStatus.COMPLETED
        await db.flush()
        return run

    pdf_bytes, pdf_hash = await acquire_pdf(
        source, auth_cookies=(auth_state or {}).get("cookies"))
    outline = await asyncio.to_thread(_outline_for, pdf_bytes)
    page_texts = await asyncio.to_thread(_page_texts, pdf_bytes)

    converted = pdf_cache.load(pdf_hash, page_texts)
    if converted is None:
        # No cache (older run, or the PDF changed) — rebuild via a full conversion
        # and re-escalate every flagged page under the normal budget, since the
        # prior escalations weren't preserved.
        logger.info("retry_escalation: no cache for %s; re-converting + full escalation", pdf_hash)
        converted = await convert_pdf(pdf_bytes)
        only: "set[int] | None" = None
        budget: "int | None" = None
    else:
        only = pages_in_ranges(pending_ranges)
        budget = max(1, len(only))  # drain all pending pages (no per-run % cap)

    async def _on_escalate(done: int, total: int, _page0: int) -> None:
        run.articles_extracted = done
        run.articles_total = total
        await db.commit()
        await service._raise_if_controlled(db, run_pk)

    failed_ranges = await escalate_low_confidence_pages(
        pdf_bytes, converted, budget=budget, only=only, on_page=_on_escalate)

    rendered_segments = await asyncio.to_thread(split_into_segments, converted, outline)
    return await _persist_segments(
        service, db, source, run, run_pk, converted, outline,
        rendered_segments, pdf_hash, failed_ranges,
    )
