"""PDF source import: acquire a PDF, segment it on natural boundaries, convert
each segment to markdown, and persist articles through the existing diff path."""
from __future__ import annotations

import collections
import hashlib
import json
import logging
import os
import re
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

import fitz  # PyMuPDF
import httpx
import pymupdf4llm
from sqlalchemy import delete, func, select, update

from app.core.config import settings
from app.models.article import Article
from app.models.extraction_run import ExtractionRun, RunStatus
from app.models.source import DocumentationSource, SourceStatus
from app.models.toc import TOCEntry
from app.services.profiles import llm as llm_mod
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


@dataclass
class RenderedImage:
    filename: str   # content-addressed: "<sha16>.png"
    data: bytes
    alt: str


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


_IMG_MARKER = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<target>[^)]+)\)")


def _render_segment(doc: "fitz.Document", segment: Segment) -> tuple[str, list[RenderedImage]]:
    """Render a segment to clean markdown, content-addressing any images.

    Images are written to a temp dir by pymupdf4llm, then each marker is rewritten
    to a bare ``<sha>.png`` canonical reference and the bytes collected — so the
    markdown is stable across runs/page-shifts and identical figures dedupe."""
    pages = list(range(segment.page_start, segment.page_end + 1))
    images: list[RenderedImage] = []
    seen: dict[str, str] = {}  # original target -> canonical filename
    seen_shas: set[str] = set()  # canonical filenames already collected
    with tempfile.TemporaryDirectory() as image_dir:
        md = pymupdf4llm.to_markdown(
            doc, pages=pages, write_images=True,
            image_path=image_dir, image_format="png",
        ) or ""

        def _replace(m: "re.Match") -> str:
            target = m.group("target")
            alt = m.group("alt")
            path = os.path.join(image_dir, os.path.basename(target))
            if not os.path.isfile(path):
                return m.group(0)  # not a written image — leave untouched
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

        md = _IMG_MARKER.sub(_replace, md)
    return sanitize_markdown(md), images


def segment_to_markdown(pdf_bytes: bytes, segment: Segment) -> str:
    """Render a segment's page range to clean markdown (without images)."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        md, _images = _render_segment(doc, segment)
        return md
    finally:
        doc.close()


def render_segments(
    pdf_bytes: bytes, segments: list[Segment]
) -> list[tuple[str, list[RenderedImage]]]:
    """Render every segment, opening the PDF once. Returns (markdown, images)
    per segment, aligned with ``segments``."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        return [_render_segment(doc, seg) for seg in segments]
    finally:
        doc.close()


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

    # Segment + build the TOC tree (delete-and-rebuild, like the web path).
    segments = await segment_pdf_async(pdf_bytes)
    run.current_phase = "pdf_convert"
    run.articles_total = len(segments)
    await db.commit()

    await db.execute(delete(TOCEntry).where(TOCEntry.source_id == source.id))
    await db.flush()

    # parent via a level stack: each segment's parent is the nearest preceding
    # entry with a strictly smaller level.
    entry_ids: list[uuid.UUID] = []
    levels: list[int] = []
    article_inputs: list[tuple] = []  # (toc_id, sort_order, title, topic_key, url, md)
    # Disambiguate colliding topic keys within this run: two sibling sections
    # sharing a title slug to the same key, which process_article_result matches
    # on — without this the later one overwrites the earlier (silent data loss).
    # First occurrence keeps the clean slug; duplicates get -2, -3, … Stable as
    # long as section order is stable, so incremental diffs stay stable.
    key_counts: dict[str, int] = {}
    # Render every segment up front, opening the PDF once rather than once per
    # segment.
    rendered = render_segments(pdf_bytes, segments)
    for i, seg in enumerate(segments):
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
        article_inputs.append((toc.id, i, seg.title, topic_key, url, rendered[i][0], rendered[i][1]))

    run.current_phase = "content_scraping"
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
