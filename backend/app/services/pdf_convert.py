"""Whole-document PDF→markdown conversion via docling-serve, with a pymupdf
fallback, plus heading-boundary splitting into article segments.

Converting the whole document at once preserves reading order and keeps tables
whole across page breaks; splitting happens later at heading boundaries (never
page ranges), which eliminates the cross-section bleed of the old page-range
pipeline."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import fitz  # PyMuPDF
import pymupdf4llm

from app.core.config import settings

if TYPE_CHECKING:
    from app.services.pdf_import import Segment
from app.services import docling_client
from app.services.docling_client import DoclingServeError, _PAGE_BREAK
from app.services.sanitize import sanitize_markdown

logger = logging.getLogger(__name__)


@dataclass
class RenderedImage:
    filename: str   # content-addressed: "<sha16>.png"
    data: bytes
    alt: str


@dataclass
class DocHeading:
    text: str
    level: int
    page0: int  # 0-based page where the heading appears


@dataclass
class ConvertedDoc:
    markdown: str
    headings: list[DocHeading]
    page_texts: list[str]
    table_pages: set[int]
    images: list[RenderedImage] = field(default_factory=list)
    engine: str = "docling"
    page_line_starts: list[int] = field(default_factory=list)


# ── image content-addressing ────────────────────────────────────────────────

_DATA_URI_RE = re.compile(
    r"!\[(?P<alt>[^\]]*)\]\(data:image/[A-Za-z0-9.+-]+;base64,(?P<b64>[A-Za-z0-9+/=\s]+)\)"
)
_IMG_MARKER = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<target>[^)]+)\)")


def _content_address_data_uris(markdown: str) -> tuple[str, list[RenderedImage]]:
    """Rewrite embedded data-URI image markers to content-addressed <sha>.png."""
    images: list[RenderedImage] = []
    seen: set[str] = set()

    def _replace(m: "re.Match") -> str:
        b64 = "".join(m.group("b64").split())
        try:
            data = base64.b64decode(b64)
        except Exception:  # noqa: BLE001 - leave malformed URIs untouched
            return m.group(0)
        sha = hashlib.sha256(data).hexdigest()[:16]
        filename = f"{sha}.png"
        if sha not in seen:
            seen.add(sha)
            images.append(RenderedImage(filename=filename, data=data, alt=m.group("alt")))
        return f"![{m.group('alt')}]({filename})"

    return _DATA_URI_RE.sub(_replace, markdown), images


def _content_address_files(markdown: str, image_dir: str) -> tuple[str, list[RenderedImage]]:
    """Rewrite file-path image markers (pymupdf4llm fallback) to <sha>.png."""
    images: list[RenderedImage] = []
    seen: dict[str, str] = {}
    seen_shas: set[str] = set()

    def _replace(m: "re.Match") -> str:
        target = m.group("target")
        alt = m.group("alt")
        if target.startswith("data:"):
            return m.group(0)
        path = os.path.join(image_dir, os.path.basename(target))
        if not os.path.isfile(path):
            return m.group(0)
        if target in seen:
            return f"![{alt}]({seen[target]})"
        with open(path, "rb") as fh:
            data = fh.read()
        filename = hashlib.sha256(data).hexdigest()[:16] + ".png"
        seen[target] = filename
        if filename not in seen_shas:
            seen_shas.add(filename)
            images.append(RenderedImage(filename=filename, data=data, alt=alt))
        return f"![{alt}]({filename})"

    return _IMG_MARKER.sub(_replace, markdown), images


# ── conversion ──────────────────────────────────────────────────────────────

def _page_texts(pdf_bytes: bytes) -> list[str]:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        return [page.get_text("text") for page in doc]
    finally:
        doc.close()


def _page_count(pdf_bytes: bytes) -> int:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        return doc.page_count
    finally:
        doc.close()


def _page_batches(page_count: int, size: int) -> list[tuple[int, int]]:
    """1-based inclusive page ranges of at most `size` pages."""
    return [(s + 1, min(s + size, page_count)) for s in range(0, page_count, size)]


def _merge_docling_docs(batch_docs: list[dict]) -> dict:
    """Stitch per-batch docling result dicts into one. docling reports absolute
    page numbers, so texts/tables concatenate without offset; markdowns join with
    the page-break placeholder so the merged page stream stays continuous."""
    mds = [(d.get("md_content") or "") for d in batch_docs]
    texts: list = []
    tables: list = []
    for d in batch_docs:
        jc = d.get("json_content") or {}
        texts.extend(jc.get("texts") or [])
        tables.extend(jc.get("tables") or [])
    return {
        "md_content": ("\n" + _PAGE_BREAK + "\n").join(mds),
        "json_content": {"texts": texts, "tables": tables},
    }


async def _convert_docling_batched(pdf_bytes: bytes, page_count: int, on_poll=None) -> dict:
    batch_docs: list[dict] = []
    for start, end in _page_batches(page_count, settings.pdf_convert_batch_pages):
        doc = await docling_client.convert_async(
            pdf_bytes, page_range=(start, end), image_export_mode="embedded",
            page_break_placeholder=_PAGE_BREAK, on_poll=on_poll,
        )
        batch_docs.append(doc)
    return _merge_docling_docs(batch_docs)


def _parse_headings(json_content: dict) -> list[DocHeading]:
    out: list[DocHeading] = []
    for item in (json_content.get("texts") or []):
        if item.get("label") not in ("section_header", "title"):
            continue
        text = (item.get("text") or "").strip()
        if not text:
            continue
        prov = item.get("prov") or []
        page0 = (prov[0].get("page_no", 1) - 1) if prov else 0
        level = 1 if item.get("label") == "title" else int(item.get("level") or 1)
        out.append(DocHeading(text=text, level=level, page0=page0))
    return out


def _parse_table_pages(json_content: dict) -> set[int]:
    pages: set[int] = set()
    for t in (json_content.get("tables") or []):
        prov = t.get("prov") or []
        if prov:
            pages.add(prov[0].get("page_no", 1) - 1)
    return pages


def _split_page_breaks(markdown: str) -> tuple[str, list[int]]:
    """Remove page-break placeholder lines; return (clean_md, page_line_starts)
    where page_line_starts[p] is the clean-markdown line index of page p (0-based)."""
    out: list[str] = []
    starts: list[int] = [0]
    for ln in markdown.split("\n"):
        if ln.strip() == _PAGE_BREAK:
            starts.append(len(out))
        else:
            out.append(ln)
    return "\n".join(out), starts


def _build_converted_doc(doc: dict, pdf_bytes: bytes) -> ConvertedDoc:
    md = doc.get("md_content") or ""
    json_content = doc.get("json_content") or {}
    md, page_line_starts = _split_page_breaks(md)
    md, images = _content_address_data_uris(md)
    return ConvertedDoc(
        markdown=sanitize_markdown(md),
        headings=_parse_headings(json_content),
        page_texts=_page_texts(pdf_bytes),
        table_pages=_parse_table_pages(json_content),
        images=images,
        engine="docling",
        page_line_starts=page_line_starts,
    )


def _convert_pymupdf(pdf_bytes: bytes) -> ConvertedDoc:
    """Whole-doc pymupdf4llm conversion (no page ranges → no boundary bleed)."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        with tempfile.TemporaryDirectory() as image_dir:
            md = pymupdf4llm.to_markdown(
                doc, write_images=True, image_path=image_dir, image_format="png"
            ) or ""
            md, images = _content_address_files(md, image_dir)
    finally:
        doc.close()
    return ConvertedDoc(
        markdown=sanitize_markdown(md), headings=[], page_texts=_page_texts(pdf_bytes),
        table_pages=set(), images=images, engine="pymupdf", page_line_starts=[],
    )


async def convert_pdf(pdf_bytes: bytes, on_poll=None) -> ConvertedDoc:
    """Convert a whole PDF to markdown via docling-serve (async); pymupdf on failure.
    Large PDFs are converted in page-range batches so docling-serve doesn't OOM.
    All heavy synchronous work runs off the event loop so the worker heartbeat
    keeps ticking on large documents."""
    if settings.pdf_converter == "pymupdf":
        return await asyncio.to_thread(_convert_pymupdf, pdf_bytes)
    try:
        page_count = await asyncio.to_thread(_page_count, pdf_bytes)
        if page_count > settings.pdf_convert_batch_pages:
            doc = await _convert_docling_batched(pdf_bytes, page_count, on_poll)
        else:
            doc = await docling_client.convert_async(
                pdf_bytes, image_export_mode="embedded",
                page_break_placeholder=_PAGE_BREAK, on_poll=on_poll,
            )
        if not (doc.get("md_content") or "").strip():
            raise DoclingServeError("empty markdown")
        return await asyncio.to_thread(_build_converted_doc, doc, pdf_bytes)
    except DoclingServeError as exc:
        logger.warning("docling-serve failed (%s); falling back to pymupdf", exc)
        return await asyncio.to_thread(_convert_pymupdf, pdf_bytes)


# ── splitting by heading boundaries ─────────────────────────────────────────

@dataclass
class RenderedSegment:
    title: str
    level: int
    path: list[str]
    page_start: int
    page_end: int
    markdown: str
    images: list[RenderedImage] = field(default_factory=list)


_ATX_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


def _heading_lines(lines: list[str]) -> list[tuple[int, str]]:
    out = []
    for i, ln in enumerate(lines):
        m = _ATX_RE.match(ln.strip())
        if m:
            out.append((i, m.group(2).strip()))
    return out


def _norm_core(s: str) -> str:
    """Match key: unescape entities, casefold, strip all non-alphanumerics.
    Makes '1Preface' == '1 Preface' and "What's" == 'What's'."""
    return re.sub(r"[^a-z0-9]+", "", html.unescape(s).lower())


def _find_heading_line(headings: list[tuple[int, str]], title: str, start: int) -> "int | None":
    t = _norm_core(title)
    if not t:
        return None
    # exact-core match first (safest), then containment as a secondary.
    for idx, text in headings:
        if idx >= start and _norm_core(text) == t:
            return idx
    for idx, text in headings:
        if idx < start:
            continue
        h = _norm_core(text)
        if t in h or h in t:
            return idx
    return None


def split_into_segments(converted: ConvertedDoc, outline: "list[Segment]") -> list[RenderedSegment]:
    md = converted.markdown
    lines = md.split("\n")
    heading_lines = _heading_lines(lines)

    # boundary tuples: (line_index, title, level, path, page_start, page_end)
    boundaries: list[tuple[int, str, int, list[str], int, int]] = []
    if outline:
        cursor = 0
        starts = converted.page_line_starts
        for seg in outline:
            line = _find_heading_line(heading_lines, seg.title, cursor)
            if line is None:
                # Never drop: fall back to the page where the entry begins.
                if starts and 0 <= seg.page_start < len(starts):
                    line = max(starts[seg.page_start], cursor)
                else:
                    line = cursor
                logger.info("split: %r not found as heading; page-fallback line %d",
                            seg.title, line)
            cursor = line + 1
            boundaries.append((line, seg.title, seg.level, seg.path or [seg.title],
                               seg.page_start, seg.page_end))
    elif converted.headings:
        cursor = 0
        stack: list[str] = []
        for h in converted.headings:
            line = _find_heading_line(heading_lines, h.text, cursor)
            if line is None:
                continue
            cursor = line + 1
            stack = stack[: h.level - 1]
            stack.append(h.text)
            boundaries.append((line, h.text, h.level, list(stack), h.page0, h.page0))
    if not boundaries and heading_lines:
        for idx, text in heading_lines:
            boundaries.append((idx, text, 1, [text], 0, 0))

    if not boundaries:
        return [RenderedSegment(
            title="Document", level=1, path=[], page_start=0,
            page_end=max(0, len(converted.page_texts) - 1),
            markdown=md.strip(), images=list(converted.images),
        )]

    segs: list[RenderedSegment] = []
    for i, (line, title, level, path, p_start, p_end) in enumerate(boundaries):
        end_line = boundaries[i + 1][0] if i + 1 < len(boundaries) else len(lines)
        body = "\n".join(lines[line:end_line]).strip()
        segs.append(RenderedSegment(
            title=title, level=level, path=path,
            page_start=p_start, page_end=p_end,
            markdown=body,
            images=[img for img in converted.images if img.filename in body],
        ))
    return segs
