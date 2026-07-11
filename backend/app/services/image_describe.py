"""VLM image-description enrichment.

evaluate_image  — dimension/size selection over raw bytes (sync; wrap in to_thread).
describe_image  — one OpenAI-compatible vision call → ImageDescription | None (Task 3).
inject_caption  — idempotent caption injection into markdown (Task 4).
enrich_run_images — the per-run enrichment phase (Task 5).
"""

import asyncio
import base64
import hashlib
import io
import json
import logging
import mimetypes
import os
import uuid
from dataclasses import dataclass

import httpx
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.article import Article
from app.models.image import ArticleImage
# Aliased: the ORM cache model, distinct from the ImageDescription dataclass
# (describe_image's return type) defined later in this module.
from app.models.image_description import ImageDescription as ImageDescriptionCache
from app.services import change_log

logger = logging.getLogger(__name__)


@dataclass
class ImageEval:
    is_meaningful: bool
    width: int | None
    height: int | None
    bytes_sha256: str


def evaluate_image(data: bytes) -> ImageEval:
    """Classify raw image bytes as meaningful (worth describing) or decorative.

    Boilerplate (skins/ui-icons/spacers) is already filtered at download time, so
    this only screens by size and pixel dimensions. Non-raster (e.g. SVG) or
    corrupt bytes can't be measured → treated as not meaningful."""
    sha = hashlib.sha256(data).hexdigest()
    if len(data) < settings.image_min_bytes:
        return ImageEval(False, None, None, sha)
    try:
        from PIL import Image  # local import: Pillow is only needed here
        with Image.open(io.BytesIO(data)) as img:
            w, h = img.size
    except Exception:  # noqa: BLE001 — unreadable/non-raster → not meaningful
        return ImageEval(False, None, None, sha)
    meaningful = w >= settings.image_min_dimension and h >= settings.image_min_dimension
    return ImageEval(meaningful, w, h, sha)


_KINDS = {"screenshot", "diagram", "chart", "photo", "other"}

_PROMPT = (
    "You are describing an image from software product documentation so it can be "
    "found by search. If it is a screenshot, state which screen or dialog it shows "
    "and the action or state depicted. If it is a diagram or chart, name the "
    "components and their relationships or the trend shown. Be concise (1-3 "
    "sentences). Ignore window chrome, browser frames, and decorative borders. "
    'Respond as JSON: {"description": "...", "kind": "screenshot|diagram|chart|photo|other"}.'
)


@dataclass
class ImageDescription:
    text: str
    kind: str


async def describe_image(
    data: bytes, alt_text: str | None, *, mime: str = "image/png", client: "httpx.AsyncClient | None" = None
) -> "ImageDescription | None":
    """Describe one image via the configured vision endpoint. Returns None on
    missing key / HTTP error / parse failure (never raises)."""
    if not settings.image_vlm_api_key:
        return None
    b64 = base64.b64encode(data).decode("ascii")
    text_part = _PROMPT if not alt_text else f"{_PROMPT}\n\nAuthor's alt text: {alt_text}"
    body = {
        "model": settings.image_vlm_model,
        "max_tokens": settings.image_vlm_max_tokens,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": text_part},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ],
        }],
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {settings.image_vlm_api_key}", "content-type": "application/json"}
    own = client is None
    c = client or httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))
    try:
        resp = await c.post(settings.image_vlm_base_url, headers=headers, json=body)
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        obj = json.loads(content)
        text = (obj.get("description") or "").strip()
        if not text:
            return None
        kind = obj.get("kind")
        return ImageDescription(text=text, kind=kind if kind in _KINDS else "other")
    except Exception as exc:  # noqa: BLE001
        logger.warning("describe_image failed: %s", exc)
        return None
    finally:
        if own:
            await c.aclose()


def _caption_block(description: str) -> str:
    # Single-line blockquote caption (description is already 1-3 sentences).
    one_line = " ".join(description.split())
    return f"> **Figure:** {one_line}"


def inject_caption(markdown: str, image_url: str, description: str) -> str:
    """Insert (or replace) a '> **Figure:** …' caption immediately after the first
    markdown image reference to *image_url*. Idempotent for a given (image, text)."""
    lines = markdown.split("\n")
    # Find the line bearing the image reference `](image_url)`.
    needle = f"]({image_url})"
    idx = next((i for i, ln in enumerate(lines) if needle in ln), None)
    if idx is None:
        return markdown

    block = _caption_block(description)
    # Determine the insertion point: right after the image line, skipping one blank.
    j = idx + 1
    if j < len(lines) and lines[j].strip() == "":
        j += 1
    # Replace an existing caption block for this image (a run of '> ' lines) if present.
    if j < len(lines) and lines[j].lstrip().startswith("> **Figure:**"):
        end = j
        while end < len(lines) and lines[end].lstrip().startswith(">"):
            end += 1
        lines[j:end] = [block]
        return "\n".join(lines)

    # Insert a fresh caption with a blank line on each side.
    insert_at = idx + 1
    lines[insert_at:insert_at] = ["", block]
    return "\n".join(lines)


async def enrich_run_images(db: AsyncSession, source_id: uuid.UUID, run_id: uuid.UUID, *, describe=describe_image) -> None:
    """Enrichment phase: describe meaningful images of this source's articles, inject
    captions, and emit an 'updated' content_changes row per enriched article. Best-effort
    — never raises into extraction; leaves content_hash and versions untouched."""
    if not settings.image_vlm_enabled:
        return
    try:
        budget = settings.image_vlm_max_per_run
        consecutive_failures = 0
        media_root = os.path.abspath(settings.media_dir)

        # Articles of this source with at least one image needing work.
        need = or_(
            ArticleImage.is_meaningful.is_(None),
            (ArticleImage.is_meaningful.is_(True)) & (ArticleImage.description.is_(None)),
        )
        art_ids = (await db.execute(
            select(Article.id)
            .where(Article.source_id == source_id)
            .where(Article.id.in_(select(ArticleImage.article_id).where(need)))
        )).scalars().all()

        for art_id in art_ids:
            article = await db.get(Article, art_id)
            if article is None:
                continue
            imgs = (await db.execute(
                select(ArticleImage).where(ArticleImage.article_id == art_id, need)
                .order_by(ArticleImage.sort_order)
            )).scalars().all()
            changed = False
            for img in imgs:
                path = os.path.join(media_root, str(art_id), img.local_filename)
                if not os.path.isfile(path):
                    continue
                data = await asyncio.to_thread(_read_file, path)
                if img.is_meaningful is None:
                    ev = await asyncio.to_thread(evaluate_image, data)
                    img.is_meaningful = ev.is_meaningful
                    img.width, img.height, img.bytes_sha256 = ev.width, ev.height, ev.bytes_sha256
                if not img.is_meaningful:
                    continue
                # Resolve a description: cache first, then VLM (budget + breaker).
                cached = await db.get(ImageDescriptionCache, img.bytes_sha256)
                if cached is not None:
                    text, kind = cached.description, cached.kind or "other"
                else:
                    if budget <= 0 or consecutive_failures >= settings.image_vlm_max_consecutive_failures:
                        continue
                    mime = mimetypes.guess_type(img.local_filename)[0] or "image/png"
                    res = await describe(data, img.alt_text, mime=mime)
                    if res is None:
                        consecutive_failures += 1
                        continue
                    consecutive_failures = 0
                    budget -= 1
                    db.add(ImageDescriptionCache(
                        bytes_sha256=img.bytes_sha256, description=res.text,
                        kind=res.kind, model=settings.image_vlm_model,
                    ))
                    text, kind = res.text, res.kind
                img.description, img.kind = text, kind
                article.content_markdown = inject_caption(article.content_markdown, img.local_path, text)
                changed = True

            if changed:
                await change_log.record_change(db, article=article, change_type="updated", run_id=run_id)
            await db.commit()
            if consecutive_failures >= settings.image_vlm_max_consecutive_failures:
                break
    except Exception as exc:  # noqa: BLE001 — enrichment is best-effort
        logger.warning("enrich_run_images failed for source %s: %s", source_id, exc)


def _read_file(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()
