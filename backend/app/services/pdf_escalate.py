"""Confidence scoring + VLM re-conversion of low-confidence PDF segments.

The standard docling-serve conversion is good but not perfect on the hardest
tables. score_segment flags segments worth re-doing; escalate_segment re-converts
them via docling-serve's VLM pipeline (pointed at OpenRouter)."""
from __future__ import annotations

import asyncio
import logging
import math
import re

from app.core.config import settings
from app.services.pdf_convert import ConvertedDoc, RenderedSegment, _extract_page_range

logger = logging.getLogger(__name__)

_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_SEP_RE = re.compile(r"^\s*\|?\s*:?-{3,}.*$")


def _cell_count(row: str) -> int:
    return len(row.strip().strip("|").split("|"))


def _has_ragged_table(md: str) -> bool:
    lines = md.split("\n")
    i, n = 0, len(md.split("\n"))
    while i < n:
        if _TABLE_ROW_RE.match(lines[i]):
            block = []
            while i < n and _TABLE_ROW_RE.match(lines[i]):
                block.append(lines[i])
                i += 1
            if len(block) < 2:
                return True
            header_cells = _cell_count(block[0])
            body = [b for b in block[2:] if not _SEP_RE.match(b)]
            if not body:
                return True
            if any(_cell_count(r) != header_cells for r in body):
                return True
            continue
        i += 1
    return False


def score_segment(segment: RenderedSegment, converted: ConvertedDoc) -> list[str]:
    issues: list[str] = []
    md = segment.markdown

    if _has_ragged_table(md):
        issues.append("ragged_table")

    seg_pages = range(segment.page_start, segment.page_end + 1)
    if any(p in converted.table_pages for p in seg_pages) and "|" not in md:
        issues.append("missing_table")

    raw = "".join(
        converted.page_texts[p] for p in seg_pages
        if 0 <= p < len(converted.page_texts)
    )
    if len(raw) > 200 and len(md) < 0.5 * len(raw):
        issues.append("sparse_text")

    return issues


from app.services import docling_client
from app.services.docling_client import DoclingServeError
from app.services.sanitize import sanitize_markdown


async def escalate_segment(pdf_bytes: bytes, segment: RenderedSegment) -> "str | None":
    """Re-convert one segment via docling-serve's VLM pipeline (OpenRouter).
    Returns the re-converted markdown (which may equal the original when the VLM
    adds nothing), or ``None`` when docling-serve itself failed — so the caller
    can tell a genuine service failure apart from a no-op improvement and trip
    its circuit breaker."""
    try:
        # Extract the segment's pages into a standalone PDF and send THAT, rather
        # than the whole document + a page_range option: docling-serve rejects a
        # large full-doc upload as "Input document is not valid" (the same reason
        # the batched conversion extracts each batch — see _extract_page_range).
        page_bytes = await asyncio.to_thread(
            _extract_page_range, pdf_bytes, segment.page_start + 1, segment.page_end + 1)
        doc = await docling_client.convert_async(
            page_bytes,
            use_vlm_api=True,
            image_export_mode="placeholder",
        )
    except DoclingServeError as exc:
        logger.warning("VLM escalation failed for %r: %s", segment.title, exc)
        return None

    cleaned = sanitize_markdown((doc.get("md_content") or "").strip())
    if not cleaned.strip():
        return segment.markdown
    if not cleaned.lstrip().startswith("#"):
        hashes = "#" * max(1, segment.level)
        cleaned = f"{hashes} {segment.title}\n\n{cleaned}"
    return cleaned


async def escalate_segments(pdf_bytes, segments, converted, on_event=None) -> list[int]:
    """Re-convert low-confidence segments in place via the VLM, but only those
    that exclusively own their page range (escalating a shared page would pull in
    neighbours' content and reintroduce cross-section bleed). Bounded by a per-run
    page budget sized as a percentage of the document's total pages
    (``pdf_vlm_max_pages_pct``), so the allowance scales with document size.

    Returns the indices (into ``segments``) of flagged segments that genuinely
    FAILED to escalate — a docling-serve error, or skipped because the circuit
    breaker tripped. Budget-deferred segments are NOT failures and are excluded,
    so a healthy big document doesn't get flagged just for hitting the page cap.
    The caller can persist these so the escalation can be retried later without
    redoing the whole conversion."""
    if not settings.pdf_vlm_escalation_enabled:
        return []

    page_owners: dict[int, int] = {}
    for s in segments:
        for p in range(s.page_start, s.page_end + 1):
            page_owners[p] = page_owners.get(p, 0) + 1

    def _exclusive(s) -> bool:
        return all(page_owners.get(p, 0) == 1 for p in range(s.page_start, s.page_end + 1))

    # Keep each flagged segment's index into the original list so the caller can
    # map a failure back to its persisted article.
    flagged = [
        (i, s) for i, s in enumerate(segments)
        if _exclusive(s) and score_segment(s, converted)
    ]
    # Budget = a percentage of the document's total pages (rounded up, min 1), so
    # a large guide gets a proportionally larger allowance than a fixed cap.
    total_pages = len(converted.page_texts) or (
        max((s.page_end for s in segments), default=-1) + 1)
    budget = max(1, math.ceil(total_pages * settings.pdf_vlm_max_pages_pct / 100.0))
    if flagged:
        logger.info(
            "pdf_escalate: %d/%d segments flagged; re-converting via VLM "
            "(budget %d/%d pages, %.0f%%)",
            len(flagged), len(segments), budget, total_pages,
            settings.pdf_vlm_max_pages_pct,
        )
    total = len(flagged)
    done = 0
    consecutive_failures = 0
    failed: list[int] = []
    for pos, (idx, seg) in enumerate(flagged):
        pages = seg.page_end - seg.page_start + 1
        if pages > budget:
            continue
        new_md = await escalate_segment(pdf_bytes, seg)
        if new_md is None:
            # docling-serve failed this conversion. A run of these means the VLM
            # pipeline is down — stop rather than hammer the shared service with
            # dozens of doomed calls. The segment keeps its standard-pipeline
            # markdown. A failed attempt converts no pages, so the budget is not
            # consumed.
            consecutive_failures += 1
            failed.append(idx)
            if consecutive_failures >= settings.pdf_vlm_max_consecutive_failures:
                logger.warning(
                    "pdf_escalate: %d consecutive VLM failures — skipping the "
                    "remaining %d flagged segments", consecutive_failures,
                    total - done - consecutive_failures,
                )
                # The remaining flagged segments weren't attempted because the
                # service is clearly down — treat them as pending too.
                failed.extend(j for j, _ in flagged[pos + 1:])
                break
            continue
        consecutive_failures = 0
        if new_md != seg.markdown:
            seg.markdown = new_md
            matched = [img for img in converted.images if img.filename in new_md]
            seg.images = matched or seg.images
        budget -= pages
        done += 1
        logger.info("pdf_escalate: %d/%d re-converted (%r)", done, total, seg.title)
        if on_event is not None:
            try:
                await on_event(done, total, seg.title)
            except Exception:  # noqa: BLE001
                logger.exception("escalate on_event failed")
    return failed
