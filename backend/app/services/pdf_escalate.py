"""Confidence scoring + VLM re-conversion of low-confidence PDF segments.

The standard docling-serve conversion is good but not perfect on the hardest
tables. score_segment flags segments worth re-doing; escalate_segment re-converts
them via docling-serve's VLM pipeline (pointed at OpenRouter)."""
from __future__ import annotations

import logging
import re

from app.services.pdf_convert import ConvertedDoc, RenderedSegment

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
