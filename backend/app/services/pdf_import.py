"""PDF source import: acquire a PDF, segment it on natural boundaries, convert
each segment to markdown, and persist articles through the existing diff path."""
from __future__ import annotations

import collections
import hashlib
import json
import logging
import os
from dataclasses import dataclass, field

import fitz  # PyMuPDF
import httpx

from app.core.config import settings
from app.services.profiles import llm as llm_mod

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


# ── Async production entry point (outline → LLM → heuristic → single) ────────


def _full_text_with_pages(doc: "fitz.Document") -> list[str]:
    """Per-page plain text (index = 0-based page number)."""
    return [page.get_text("text") for page in doc]


async def _llm_segment_titles(text: str) -> list[dict]:
    """Ask the configured LLM for an ordered list of {title, level} section
    headings. Returns [] on any failure (caller falls back to heuristic)."""
    prompt = (
        "You are given the plain text of a documentation PDF. Identify its "
        "section headings in reading order. Respond with ONLY a JSON array of "
        'objects like {"title": "...", "level": 1}, where level 1 is a top '
        "section and deeper levels are subsections. No prose.\n\n"
        f"TEXT:\n{text[:24000]}"
    )
    try:
        raw = await llm_mod.call_llm(prompt)
        data = json.loads(llm_mod._strip_fences(raw))
        out = []
        for item in data:
            t = str(item.get("title", "")).strip()
            if t:
                out.append({"title": t, "level": int(item.get("level", 1) or 1)})
        return out
    except Exception as exc:  # noqa: BLE001 - fallback is intentional
        logger.warning("LLM segmentation failed, falling back: %s", exc)
        return []


def _titles_to_segments(doc: "fitz.Document", titles: list[dict]) -> list[Segment]:
    pages = _full_text_with_pages(doc)
    last_page = doc.page_count - 1
    located: list[tuple[int, str, int]] = []  # (page0, title, level)
    for item in titles:
        title = item["title"]
        page0 = next((i for i, t in enumerate(pages) if title in t), None)
        if page0 is not None:
            located.append((page0, title, item["level"]))
    if not located:
        return []
    segs: list[Segment] = []
    stack: list[str] = []
    for i, (pno, title, level) in enumerate(located):
        end = located[i + 1][0] - 1 if i + 1 < len(located) else last_page
        end = max(pno, end)
        stack = stack[: level - 1]
        stack.append(title)
        segs.append(Segment(title=title, level=level, page_start=pno,
                            page_end=end, path=list(stack)))
    return segs


async def segment_pdf_async(pdf_bytes: bytes) -> list[Segment]:
    """Production segmenter: outline-first, then LLM (when enabled), then the
    font-size heuristic, then a single whole-document segment."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        segs = _outline_segments(doc)
        if segs:
            return segs
        if settings.llm_fallback_enabled:
            text = "\n".join(_full_text_with_pages(doc))
            titles = await _llm_segment_titles(text)
            segs = _titles_to_segments(doc, titles)
            if segs:
                return segs
        segs = heuristic_segments(doc)
        if segs:
            return segs
        return [Segment(title="Document", level=1, page_start=0,
                        page_end=max(0, doc.page_count - 1), path=[])]
    finally:
        doc.close()
