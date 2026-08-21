"""Confidence scoring + VLM re-conversion of low-confidence PDF pages.

The standard docling-serve conversion is good but not perfect on the hardest
tables and on image-only pages. ``score_page`` flags an individual page worth
re-doing; ``escalate_low_confidence_pages`` re-converts just those pages via
docling-serve's VLM pipeline (pointed at OpenRouter) and splices the results back
into the ConvertedDoc — BEFORE the document is split into articles, so the split
sees the improved content (and can detect sub-sections the standard pipeline
missed). Escalating per page, rather than per whole outline section, means one bad
table no longer drags a 100+ page chapter through the VLM.
"""
from __future__ import annotations

import asyncio
import logging
import math
import re

from app.core.config import settings
from app.services.pdf_convert import (
    ConvertedDoc, RenderedImage, _content_address_data_uris, _extract_page_range,
    rebuild_from_pages, split_pages,
)

logger = logging.getLogger(__name__)

_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_SEP_RE = re.compile(r"^\s*\|?\s*:?-{3,}.*$")

# Issues that mean content is actually missing/wrong (worth surfacing as pending
# when over budget), vs a merely-cosmetic imperfection ("ragged_table") on a page
# that still has its content.
_CONTENT_LOSS_ISSUES = {"empty_pages", "sparse_text", "missing_table"}


def _report(on_error, page0: int, reason: str) -> None:
    if on_error is None:
        return
    try:
        on_error(page0, reason)
    except Exception:  # noqa: BLE001 — reporting must never break the drain
        logger.exception("escalate on_error callback failed")


def _cell_count(row: str) -> int:
    return len(row.strip().strip("|").split("|"))


def _has_ragged_table(md: str) -> bool:
    lines = md.split("\n")
    i, n = 0, len(lines)
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


def score_page(page_md: str, page_idx: int, converted: ConvertedDoc) -> list[str]:
    """Confidence issues for a SINGLE page's markdown. Empty list = trust it.

    - ``ragged_table``  : a table on the page has inconsistent column counts.
    - ``missing_table`` : docling detected a table on this page but the markdown
      has no table at all.
    - ``sparse_text``   : the page has a real text layer but the markdown captured
      less than half of it.
    - ``empty_pages``   : an image-only page (≈no text layer) that also produced
      almost no markdown — the content the VLM must read from the rendered page.
    """
    issues: list[str] = []
    md = page_md

    if _has_ragged_table(md):
        issues.append("ragged_table")

    if page_idx in converted.table_pages and "|" not in md:
        issues.append("missing_table")

    raw = converted.page_texts[page_idx] if 0 <= page_idx < len(converted.page_texts) else ""
    if len(raw) > 200 and len(md) < 0.5 * len(raw):
        issues.append("sparse_text")
    elif len(raw.strip()) < 50 and len(md.strip()) < settings.pdf_min_chars_per_page:
        # No usable text layer AND almost no markdown → an image-only page whose
        # content was lost. sparse_text can't catch this (nothing to compare to).
        issues.append("empty_pages")

    return issues


from app.services import docling_client
from app.services.docling_client import DoclingConversionFailed, DoclingServeError
from app.services.sanitize import sanitize_markdown


async def escalate_page(
    pdf_bytes: bytes, page0: int, *, on_error=None,
) -> "tuple[str, list[RenderedImage]] | None":
    """Re-convert ONE page (0-based) via docling-serve's VLM pipeline.

    Returns ``(markdown, images)`` for that page, or ``None`` when docling-serve
    itself failed — so the caller can tell a service failure from a no-op and trip
    its circuit breaker. Images are content-addressed (``<sha>.png``) exactly like
    the main conversion so figures on the page are preserved.

    ``on_error(page0, reason)`` is called with the final failure reason, so the
    run can record *why* a page is still pending instead of leaving that only in
    docling-serve's own log.
    """
    # Extract the single page into a standalone PDF and send THAT (docling
    # rejects a large full-doc upload as "Input document is not valid").
    page_bytes = await asyncio.to_thread(_extract_page_range, pdf_bytes, page0 + 1, page0 + 1)
    # docling-serve intermittently fails a page that converts fine on re-submit;
    # retry a few times before giving up so a transient wobble doesn't trip the
    # caller's consecutive-failure breaker and abandon the rest of the drain.
    attempts = 1 + max(0, settings.pdf_vlm_page_retries)
    for attempt in range(attempts):
        try:
            doc = await docling_client.convert_async(
                page_bytes, use_vlm_api=True, image_export_mode="embedded",
            )
        except DoclingConversionFailed as exc:
            # docling ran the page and its pipeline broke on it. Resubmitting the
            # same bytes fails identically, so burning the remaining attempts
            # only delays the run.
            logger.warning("VLM escalation failed for page %d (not retryable): %s",
                           page0 + 1, exc)
            _report(on_error, page0, str(exc))
            return None
        except DoclingServeError as exc:
            if attempt + 1 < attempts:
                logger.info("VLM escalation transient failure for page %d "
                            "(attempt %d/%d): %s; retrying", page0 + 1, attempt + 1,
                            attempts, exc)
                await asyncio.sleep(settings.pdf_vlm_page_retry_backoff * (attempt + 1))
                continue
            logger.warning("VLM escalation failed for page %d after %d attempts: %s",
                           page0 + 1, attempts, exc)
            _report(on_error, page0, str(exc))
            return None
        md, images = _content_address_data_uris(doc.get("md_content") or "")
        return sanitize_markdown(md), images


async def escalate_low_confidence_pages(
    pdf_bytes: bytes, converted: ConvertedDoc, *,
    budget: "int | None" = None, only: "set[int] | None" = None, on_page=None,
    failures: "dict[int, str] | None" = None,
) -> list[dict]:
    """VLM-re-convert low-confidence PAGES in place on ``converted``, BEFORE the
    document is split into articles.

    Slices ``converted.markdown`` into per-page markdown, scores each page, and
    re-converts the flagged ones (up to ``budget`` pages — a percentage of the
    document, ``pdf_vlm_max_pages_pct``), splicing each result back and rebuilding
    ``converted.markdown`` / ``converted.page_line_starts`` so the subsequent split
    sees the improved content. ``only`` restricts candidates to a specific page set
    (used by retry to target the previously-failed pages).

    Returns the pages that still need escalation — VLM failures, and over-budget
    pages whose issue is genuine content loss — as contiguous ranges
    ``[{"page_start", "page_end"}]`` for ``ExtractionRun.escalation_pending``.
    Budget-deferred *cosmetic* pages (e.g. a ragged table on populated content) are
    not surfaced, so a healthy big document doesn't warn just for hitting the cap.

    ``failures``, when given, is filled with ``{page0: reason}`` for every page
    docling refused, so the caller can record why rather than leaving the reason
    in docling-serve's log.
    """
    if not settings.pdf_vlm_escalation_enabled:
        return []
    pages = split_pages(converted)
    if not pages:
        return []  # no page offsets (pymupdf fallback) — page-level work n/a

    n = len(pages)
    total_pages = len(converted.page_texts) or n
    if budget is None:
        budget = max(1, math.ceil(total_pages * settings.pdf_vlm_max_pages_pct / 100.0))

    flagged: list[tuple[int, list[str]]] = []
    for p in range(n):
        if only is not None:
            # Retry targeting: attempt the requested pages regardless of their
            # current score (they were flagged and failed on a prior run).
            if p not in only:
                continue
            flagged.append((p, score_page(pages[p], p, converted)))
        else:
            issues = score_page(pages[p], p, converted)
            if issues:
                flagged.append((p, issues))
    if flagged:
        logger.info(
            "pdf_escalate: %d/%d pages flagged; VLM re-converting (budget %d/%d pages, %.0f%%)",
            len(flagged), n, budget, total_pages, settings.pdf_vlm_max_pages_pct,
        )

    total = len(flagged)
    done = 0
    consecutive_failures = 0
    failed_pages: list[int] = []
    changed = False
    seen = {img.filename for img in converted.images}
    new_images: list[RenderedImage] = []
    breaker = settings.pdf_vlm_max_consecutive_failures

    for pos, (p, issues) in enumerate(flagged):
        if budget <= 0 or consecutive_failures >= breaker:
            # Can't attempt more this run. Surface only genuine content loss as
            # pending (retryable); leave cosmetic-only pages deferred silently.
            if _CONTENT_LOSS_ISSUES.intersection(issues):
                failed_pages.append(p)
            continue
        result = await escalate_page(
            pdf_bytes, p,
            on_error=(None if failures is None
                      else lambda pg, reason: failures.__setitem__(pg, reason)),
        )
        if result is None:
            consecutive_failures += 1
            failed_pages.append(p)
            if consecutive_failures >= breaker:
                logger.warning(
                    "pdf_escalate: %d consecutive VLM failures — deferring the "
                    "remaining %d flagged pages", consecutive_failures,
                    total - pos - 1,
                )
            continue
        consecutive_failures = 0
        budget -= 1
        md, imgs = result
        if md.strip() and md != pages[p]:
            pages[p] = md
            changed = True
            for im in imgs:
                if im.filename not in seen:
                    seen.add(im.filename)
                    new_images.append(im)
        done += 1
        logger.info("pdf_escalate: %d/%d pages re-converted (page %d)", done, total, p + 1)
        if on_page is not None:
            try:
                await on_page(done, total, p)
            except Exception:  # noqa: BLE001
                logger.exception("escalate on_page callback failed")

    if changed:
        converted.markdown, converted.page_line_starts = rebuild_from_pages(pages)
        converted.images = list(converted.images) + new_images

    return _contiguous_ranges(failed_pages)


def _contiguous_ranges(pages: list[int]) -> list[dict]:
    """Collapse a list of 0-based page indices into sorted contiguous ranges."""
    if not pages:
        return []
    ordered = sorted(set(pages))
    ranges: list[dict] = []
    start = prev = ordered[0]
    for p in ordered[1:]:
        if p == prev + 1:
            prev = p
        else:
            ranges.append({"page_start": start, "page_end": prev})
            start = prev = p
    ranges.append({"page_start": start, "page_end": prev})
    return ranges


def pages_in_ranges(ranges: "list[dict] | None") -> set[int]:
    """Expand ``escalation_pending`` page ranges back into a set of page indices."""
    out: set[int] = set()
    for r in ranges or []:
        out.update(range(int(r["page_start"]), int(r["page_end"]) + 1))
    return out
