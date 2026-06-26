"""PDF source import: acquire a PDF, segment it on natural boundaries, convert
each segment to markdown, and persist articles through the existing diff path."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import fitz  # PyMuPDF

logger = logging.getLogger(__name__)


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


def segment_pdf(pdf_bytes: bytes) -> list[Segment]:
    """Split a PDF into ordered article segments on natural content boundaries.

    Outline-first: when the PDF carries a bookmark outline, each entry is a
    segment spanning to the page before the next same-or-higher-level entry.
    When there is no usable outline this returns a single whole-document segment
    (Tasks 4/5 layer LLM/heuristic fallbacks in front of that)."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        segs = _outline_segments(doc)
        if segs:
            return segs
        # Worst case: one segment for the whole document.
        return [Segment(title="Document", level=1, page_start=0,
                        page_end=max(0, doc.page_count - 1), path=[])]
    finally:
        doc.close()
