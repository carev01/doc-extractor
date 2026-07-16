"""On-disk cache of a converted PDF, keyed by the PDF's content hash.

Lets an escalate-retry resume from a prior conversion — re-escalating only the
leftover pages and re-splitting — WITHOUT redoing the (often >1h) Layer-A docling
conversion. Written only when a run finishes with pages still pending escalation.

Two parts, both under ``settings.pdf_cache_dir`` (a PVC, mirroring media_dir):
- ``<pdf_hash>.json`` — the converted markdown, page offsets, table pages, and
  image metadata (filename + alt).
- ``blobs/<filename>`` — content-addressed image bytes (``<sha>.png``), shared
  across articles/runs. Needed because re-splitting reassigns images to different
  article dirs, and ``process_article_result`` rewrites each article dir from the
  image bytes — so the bytes must survive independently of any article dir.

``page_texts`` and the bookmark outline are re-derived from the re-acquired PDF
(cheap fitz calls), so they are not cached.
"""
from __future__ import annotations

import json
import logging
import os

from app.core.config import settings
from app.services.pdf_convert import ConvertedDoc, RenderedImage

logger = logging.getLogger(__name__)


def _root() -> str:
    return os.path.abspath(settings.pdf_cache_dir)


def _doc_path(pdf_hash: str) -> str:
    return os.path.join(_root(), f"{pdf_hash}.json")


def _blob_dir() -> str:
    return os.path.join(_root(), "blobs")


def save(pdf_hash: str, converted: ConvertedDoc) -> None:
    """Persist the converted doc + its image bytes. Best-effort (never raises)."""
    try:
        os.makedirs(_blob_dir(), exist_ok=True)
        for im in converted.images:
            blob = os.path.join(_blob_dir(), im.filename)
            if im.data and not os.path.isfile(blob):
                with open(blob, "wb") as fh:
                    fh.write(im.data)
        payload = {
            "markdown": converted.markdown,
            "page_line_starts": converted.page_line_starts,
            "table_pages": sorted(converted.table_pages),
            "images": [{"filename": im.filename, "alt": im.alt} for im in converted.images],
        }
        tmp = _doc_path(pdf_hash) + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(payload, fh)
        os.replace(tmp, _doc_path(pdf_hash))
    except Exception as exc:  # noqa: BLE001 — cache is an optimization
        logger.warning("pdf_cache.save failed for %s: %s", pdf_hash, exc)


def load(pdf_hash: str, page_texts: list[str]) -> "ConvertedDoc | None":
    """Reconstruct a ConvertedDoc from cache, reading image bytes from the blob
    store. ``page_texts`` is re-derived by the caller from the PDF. Returns None if
    no cache entry exists (or on any read error → caller falls back to re-convert).
    An image whose blob is missing is dropped (its markdown reference just won't
    resolve to a stored figure)."""
    try:
        with open(_doc_path(pdf_hash)) as fh:
            payload = json.load(fh)
    except FileNotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("pdf_cache.load failed for %s: %s", pdf_hash, exc)
        return None

    images: list[RenderedImage] = []
    for meta in payload.get("images") or []:
        blob = os.path.join(_blob_dir(), meta["filename"])
        try:
            with open(blob, "rb") as fh:
                data = fh.read()
        except OSError:
            continue  # blob gone — skip; content still references the name harmlessly
        images.append(RenderedImage(filename=meta["filename"], data=data, alt=meta.get("alt", "")))

    return ConvertedDoc(
        markdown=payload["markdown"],
        headings=[],
        page_texts=page_texts,
        table_pages=set(payload.get("table_pages") or []),
        images=images,
        engine="docling",
        page_line_starts=payload.get("page_line_starts") or [],
    )


def delete(pdf_hash: str) -> None:
    """Drop a doc-cache entry (blobs are content-addressed/shared and left for a
    separate GC). Best-effort."""
    try:
        os.remove(_doc_path(pdf_hash))
    except FileNotFoundError:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.warning("pdf_cache.delete failed for %s: %s", pdf_hash, exc)
