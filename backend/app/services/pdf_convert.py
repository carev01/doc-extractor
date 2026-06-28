"""Whole-document PDF→markdown conversion via docling-serve, with a pymupdf
fallback, plus heading-boundary splitting into article segments.

Converting the whole document at once preserves reading order and keeps tables
whole across page breaks; splitting happens later at heading boundaries (never
page ranges), which eliminates the cross-section bleed of the old page-range
pipeline."""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field

import fitz  # PyMuPDF
import pymupdf4llm

from app.core.config import settings
from app.services import docling_client
from app.services.docling_client import DoclingServeError
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
        table_pages=set(), images=images, engine="pymupdf",
    )


async def convert_pdf(pdf_bytes: bytes) -> ConvertedDoc:
    """Convert a whole PDF to markdown. docling-serve first; pymupdf on failure."""
    if settings.pdf_converter == "pymupdf":
        return _convert_pymupdf(pdf_bytes)
    try:
        doc = await docling_client.convert(
            pdf_bytes, pipeline="standard", image_export_mode="embedded"
        )
        md = doc.get("md_content") or ""
        if not md.strip():
            raise DoclingServeError("empty markdown")
        json_content = doc.get("json_content") or {}
        md, images = _content_address_data_uris(md)
        return ConvertedDoc(
            markdown=sanitize_markdown(md),
            headings=_parse_headings(json_content),
            page_texts=_page_texts(pdf_bytes),
            table_pages=_parse_table_pages(json_content),
            images=images,
            engine="docling",
        )
    except DoclingServeError as exc:
        logger.warning("docling-serve failed (%s); falling back to pymupdf", exc)
        return _convert_pymupdf(pdf_bytes)
