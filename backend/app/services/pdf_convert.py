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


def _extract_page_range(pdf_bytes: bytes, start1: int, end1: int) -> bytes:
    """Extract a 1-based inclusive page range into a standalone PDF.

    docling-serve receives the document as a base64 payload; sending the WHOLE
    PDF on every batch (with only a page_range option) means a large document is
    uploaded in full each time and docling rejects it ("Input document is not
    valid"). Extracting just the batch's pages keeps each payload small.
    """
    src = fitz.open(stream=pdf_bytes, filetype="pdf")
    out = fitz.open()
    try:
        out.insert_pdf(src, from_page=start1 - 1, to_page=end1 - 1)
        return out.tobytes(deflate=True, garbage=3)
    finally:
        out.close()
        src.close()


def _offset_page_nos(doc: dict, offset: int) -> dict:
    """Shift docling json page numbers by ``offset`` so per-batch (page-extracted)
    results carry absolute page numbers for heading/table → page mapping."""
    if offset:
        jc = doc.get("json_content") or {}
        for item in list(jc.get("texts") or []) + list(jc.get("tables") or []):
            for p in (item.get("prov") or []):
                if isinstance(p.get("page_no"), int):
                    p["page_no"] += offset
    return doc


async def _convert_docling_batched(
    pdf_bytes: bytes, page_count: int, on_poll=None, on_progress=None
) -> dict:
    batch_docs: list[dict] = []
    images: list[RenderedImage] = []
    seen_images: set[str] = set()
    for start, end in _page_batches(page_count, settings.pdf_convert_batch_pages):
        # Send only this batch's pages (not the whole PDF + a page_range) so the
        # docling-serve payload stays small; re-base its page numbers to absolute.
        batch_bytes = await asyncio.to_thread(_extract_page_range, pdf_bytes, start, end)
        doc = await docling_client.convert_async(
            batch_bytes, image_export_mode="embedded",
            page_break_placeholder=_PAGE_BREAK, on_poll=on_poll,
        )
        _offset_page_nos(doc, start - 1)
        # Content-address this batch's images now and drop the base64 from the
        # markdown — otherwise every batch's embedded base64 is held across the
        # whole loop + the merge + a second decode in _build_converted_doc, which
        # is the peak-memory driver that OOMs the worker on image-heavy PDFs.
        clean_md, batch_imgs = _content_address_data_uris(doc.get("md_content") or "")
        doc["md_content"] = clean_md
        for im in batch_imgs:
            if im.filename not in seen_images:
                seen_images.add(im.filename)
                images.append(im)
        batch_docs.append(doc)
        # Report determinate progress as each batch finalizes: docling gives no
        # per-page progress for a single conversion, but a batched run has a
        # known page total, so the bar can advance instead of sitting at 0% for
        # the whole (often >1h) conversion.
        if on_progress is not None:
            await on_progress(end, page_count)
    merged = _merge_docling_docs(batch_docs)
    merged["images"] = images
    return merged


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
    where page_line_starts[p] is the clean-markdown line index of page p (0-based).

    MUST run last (after content-addressing and sanitize), so the returned offsets
    index the *final* markdown — sanitize collapses blank runs (concentrated at the
    page-break gaps) and would otherwise drift the offsets by ~1 line per page,
    breaking the page-anchored boundary match on long documents. Consecutive
    markers (a batch-seam artifact, or an empty page carrying no content to anchor)
    are deduped so page indices stay aligned with the source PDF's numbering."""
    out: list[str] = []
    starts: list[int] = [0]
    for ln in markdown.split("\n"):
        if ln.strip() == _PAGE_BREAK:
            if starts[-1] != len(out):
                starts.append(len(out))
        else:
            out.append(ln)
    # Anchor each page at its first non-blank line: removing the marker leaves the
    # blank lines that framed it, so a raw offset would point at a blank just before
    # the page's heading. Advancing past blanks lands the anchor on real content.
    for i, s in enumerate(starts):
        while s < len(out) and not out[s].strip():
            s += 1
        starts[i] = s
    return "\n".join(out), starts


def _build_converted_doc(doc: dict, pdf_bytes: bytes) -> ConvertedDoc:
    md = doc.get("md_content") or ""
    json_content = doc.get("json_content") or {}
    if "images" in doc:
        # Batched path already content-addressed images per batch (and stripped
        # the base64 from the markdown), to avoid holding every image twice.
        images = doc["images"]
    else:
        md, images = _content_address_data_uris(md)
    # Page-break markers (HTML comments) are carried THROUGH content-addressing and
    # sanitize — none of the sanitize rules touch comment lines — then stripped last
    # so page_line_starts indexes the final, sanitized markdown (see _split_page_breaks).
    md = sanitize_markdown(md)
    md, page_line_starts = _split_page_breaks(md)
    return ConvertedDoc(
        markdown=md,
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


async def convert_pdf(pdf_bytes: bytes, on_poll=None, on_progress=None) -> ConvertedDoc:
    """Convert a whole PDF to markdown via docling-serve (async); pymupdf on failure.
    Large PDFs are converted in page-range batches so docling-serve doesn't OOM.
    All heavy synchronous work runs off the event loop so the worker heartbeat
    keeps ticking on large documents.

    ``on_progress(pages_done, pages_total)`` is awaited after each batch finalizes
    (batched path only); a single-shot conversion has no intermediate progress."""
    if settings.pdf_converter == "pymupdf":
        return await asyncio.to_thread(_convert_pymupdf, pdf_bytes)
    try:
        page_count = await asyncio.to_thread(_page_count, pdf_bytes)
        if page_count > settings.pdf_convert_batch_pages:
            doc = await _convert_docling_batched(
                pdf_bytes, page_count, on_poll, on_progress)
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
    # Index into the source outline of the entry that STARTS this article
    # (a "detected section": a Docling heading match or a top-level chapter).
    # -1 when segmentation didn't come from an outline (docling-headings/whole-doc
    # fallbacks). The full outline drives the TOC; articles are the coarser,
    # gap-free content units each outline entry links into (see split_into_segments).
    outline_index: int = -1


_ATX_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


def _heading_lines(lines: list[str]) -> list[tuple[int, str]]:
    out = []
    for i, ln in enumerate(lines):
        m = _ATX_RE.match(ln.strip())
        if m:
            out.append((i, m.group(2).strip()))
    return out


# A line that is entirely bold (optionally behind a list marker): "**Title**",
# "__Title__", "- **Title**". Docling renders many PDF section headings this way
# rather than as ATX '#' headings.
_BOLD_ONLY_RE = re.compile(r"^(?:[-*+]\s+)?(?:\*\*|__)(.+?)(?:\*\*|__)$")
# Max visible length for a bare line to be considered a title candidate (keeps
# body sentences out of the candidate set).
_TITLE_LINE_MAX = 120


def _title_candidate_lines(lines: list[str]) -> list[tuple[int, str, bool]]:
    """Lines that could delimit a section, as (index, title_text, strong).

    ``strong`` = an ATX heading or a fully-bold line (a confident heading, so it
    is eligible for looser containment matching). Bare short lines are ``weak``:
    matched only by exact normalized equality, so body prose can't be mistaken
    for a heading. This widens boundary detection well beyond ATX headings —
    essential for PDFs (e.g. Dell manuals) whose outline is far finer than the
    ATX headings Docling emits, most titles landing as bold or plain lines."""
    out: list[tuple[int, str, bool]] = []
    for i, ln in enumerate(lines):
        s = ln.strip()
        if not s:
            continue
        m = _ATX_RE.match(s)
        if m:
            out.append((i, m.group(2).strip(), True)); continue
        mb = _BOLD_ONLY_RE.match(s)
        if mb:
            out.append((i, mb.group(1).strip(), True)); continue
        # A short standalone line with no sentence-ending punctuation reads as a
        # heading Docling left unstyled; only exact matches use it (weak).
        if len(s) <= _TITLE_LINE_MAX and s[-1] not in ".:;,!?" and s[0] not in "|>![":
            out.append((i, s, False))
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


_PAGE_WINDOW = 4          # accept a heading within N pages of its bookmark page
_FALLBACK_WINDOW = 600    # …or within N lines of the cursor when page offsets are absent
_ANCHOR_BACK = 3          # a heading may render a few lines above its page's top offset


def _match_in_window(
    candidates: list[tuple[int, str, bool]],
    title: str,
    lo: int,
    hi: int,
    anchor: int,
) -> "int | None":
    """Return the line of the heading matching *title* within ``[lo, hi]``, or None.

    An exact normalized match wins over containment; ties break by proximity to
    *anchor*. Containment is restricted to the SAFE direction — the heading is a
    substring of the title (``h in t``), which absorbs section numbering the
    heading lacks (outline '1.2 Foo' ↔ heading 'Foo'), and only for *strong*
    (ATX/bold) lines. The reverse (title ⊆ heading) is intentionally rejected: it
    matched a longer *sub*-heading (outline 'Avamar server' → heading 'Avamar
    server functional blocks'), pointing the article at the wrong place."""
    t = _norm_core(title)
    if not t:
        return None
    best: "int | None" = None
    best_key: "tuple[int, int] | None" = None
    for idx, text, strong in candidates:
        if idx < lo or idx > hi:
            continue
        h = _norm_core(text)
        if not h:
            continue
        exact = h == t
        contained = strong and (h in t)
        if not (exact or contained):
            continue
        key = (0 if exact else 1, abs(idx - anchor))
        if best_key is None or key < best_key:
            best, best_key = idx, key
    return best


def split_into_segments(converted: ConvertedDoc, outline: "list[Segment]") -> list[RenderedSegment]:
    md = converted.markdown
    lines = md.split("\n")
    heading_lines = _heading_lines(lines)

    # boundary tuples: (line_index, title, level, path, page_start, page_end, outline_idx)
    boundaries: list[tuple[int, str, int, list[str], int, int, int]] = []
    if outline:
        # ARTICLES are the coarser, gap-free content units; the full outline drives
        # the TOC separately (in pdf_import). An article begins at a "detected
        # section" — an outline entry whose title Docling actually emitted as a
        # heading, or a top-level chapter — anchored to the entry's bookmark page
        # (page numbers are authoritative; Docling's heading detection is not).
        # Finer sub-entries with no heading of their own do NOT open an article;
        # their text stays in the article that covers their page (each TOC entry
        # links to that article), so nothing is dropped and the TOC stays faithful.
        # Titles match ATX headings, bold lines, or bare title lines.
        candidates = _title_candidate_lines(lines)
        starts = converted.page_line_starts
        n_pages = len(starts)
        # For each entry, the line where the NEXT strictly-later page begins bounds
        # its heading search, so an entry never matches a heading belonging to a
        # later section (outline page_starts are non-decreasing).
        next_page_line = [len(lines)] * len(outline)
        for i in range(len(outline) - 2, -1, -1):
            nxt_ps = outline[i + 1].page_start
            if nxt_ps > outline[i].page_start and 0 <= nxt_ps < n_pages:
                next_page_line[i] = starts[nxt_ps]
            else:
                next_page_line[i] = next_page_line[i + 1]
        cursor = 0
        for i, seg in enumerate(outline):
            has_page = bool(starts) and 0 <= seg.page_start < n_pages
            if has_page:
                page_top = starts[seg.page_start]
                lo = max(cursor, page_top - _ANCHOR_BACK)
                # exclusive of the next section's page top: a heading there is the
                # NEXT entry's, never this one's.
                hi = max(lo, next_page_line[i] - 1)
                anchor = max(cursor, page_top)
                matched = _match_in_window(candidates, seg.title, lo, hi, anchor)
            else:
                # No page offsets (pymupdf fallback): match within a line window of
                # the cursor; can't page-anchor blind, so only a heading opens here.
                matched = _match_in_window(
                    candidates, seg.title, cursor, cursor + _FALLBACK_WINDOW, cursor)
            # An article starts here only for a detected section: a matched heading,
            # or a top-level chapter (major division), or the very first entry (so
            # the document's opening content is never orphaned).
            starts_article = matched is not None or seg.level == 1 or not boundaries
            if not starts_article:
                continue
            if matched is not None:
                line = matched
            elif has_page and page_top >= cursor:
                line = page_top          # level-1 chapter, no heading → page top
            else:
                line = cursor            # can't place precisely → right after prev
            if line < cursor:
                line = cursor
            boundaries.append((line, seg.title, seg.level, seg.path or [seg.title],
                               seg.page_start, seg.page_end, i))
            cursor = line + 1
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
            boundaries.append((line, h.text, h.level, list(stack), h.page0, h.page0, -1))
    if not boundaries and heading_lines:
        for idx, text in heading_lines:
            boundaries.append((idx, text, 1, [text], 0, 0, -1))

    if not boundaries:
        return [RenderedSegment(
            title="Document", level=1, path=[], page_start=0,
            page_end=max(0, len(converted.page_texts) - 1),
            markdown=md.strip(), images=list(converted.images),
        )]

    segs: list[RenderedSegment] = []
    for i, (line, title, level, path, p_start, p_end, o_idx) in enumerate(boundaries):
        end_line = boundaries[i + 1][0] if i + 1 < len(boundaries) else len(lines)
        body = "\n".join(lines[line:end_line]).strip()
        if not body:
            # A detected section with no content before the next boundary — no
            # article to emit. Its outline entry still exists in the TOC and links
            # to the previous covering article, so nothing is lost.
            continue
        segs.append(RenderedSegment(
            title=title, level=level, path=path,
            page_start=p_start, page_end=p_end,
            markdown=body,
            images=[img for img in converted.images if img.filename in body],
            outline_index=o_idx,
        ))
    if not segs:
        # No heading-delimited section held content — keep the whole document as
        # one article rather than emitting nothing.
        return [RenderedSegment(
            title="Document", level=1, path=[], page_start=0,
            page_end=max(0, len(converted.page_texts) - 1),
            markdown=md.strip(), images=list(converted.images),
        )]
    return segs
