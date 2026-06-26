"""PDF source import: acquire a PDF, segment it on natural boundaries, convert
each segment to markdown, and persist articles through the existing diff path."""
from __future__ import annotations

import collections
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


def _body_font_size(doc: "fitz.Document") -> float:
    sizes: collections.Counter = collections.Counter()
    for page in doc:
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    sizes[round(span["size"], 1)] += len(span.get("text", ""))
    return sizes.most_common(1)[0][0] if sizes else 12.0


def heuristic_segments(doc: "fitz.Document") -> list[Segment]:
    """Detect headings by font size (>= 1.25x body, short line) and split there.
    Returns [] when no headings stand out (caller falls back to single segment)."""
    body = _body_font_size(doc)
    threshold = body * 1.25
    headings: list[tuple[int, str]] = []  # (page0, title)
    for pno, page in enumerate(doc):
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                if not spans:
                    continue
                text = "".join(s.get("text", "") for s in spans).strip()
                max_size = max((s["size"] for s in spans), default=0)
                if text and len(text) <= 120 and max_size >= threshold:
                    headings.append((pno, text))
    if not headings:
        return []
    last_page = doc.page_count - 1
    segs: list[Segment] = []
    for i, (pno, title) in enumerate(headings):
        end = headings[i + 1][0] - 1 if i + 1 < len(headings) else last_page
        end = max(pno, end)
        segs.append(Segment(title=title, level=1, page_start=pno,
                            page_end=end, path=[title]))
    return segs


def segment_pdf(pdf_bytes: bytes) -> list[Segment]:
    """Split a PDF into ordered article segments on natural content boundaries.

    Outline-first: when the PDF carries a bookmark outline, each entry becomes a
    segment spanning from its page to the page before the very next outline entry
    (regardless of level) — so a parent entry covers only its own pages before its
    first child, producing a non-overlapping page partition. When there is no usable
    outline this returns a single whole-document segment (Tasks 4/5 layer
    LLM/heuristic fallbacks in front of that)."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        segs = _outline_segments(doc)
        if segs:
            return segs
        segs = heuristic_segments(doc)
        if segs:
            return segs
        return [Segment(title="Document", level=1, page_start=0,
                        page_end=max(0, doc.page_count - 1), path=[])]
    finally:
        doc.close()
