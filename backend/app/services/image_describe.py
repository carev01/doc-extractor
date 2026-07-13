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


# Raster formats a vision model can actually consume, mapped to their MIME.
# Anything else (WMF/EMF/SVG/TIFF/…) — even when mislabeled .png — is rejected
# by the VLM (400), so it is never worth describing.
_VLM_IMAGE_FORMATS = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
    "GIF": "image/gif",
}


def _detect_format(data: bytes) -> str | None:
    """PIL's format name for these bytes (e.g. 'PNG'), or None if unreadable.
    Detected from content, not the file extension — Arcserve serves some WMF
    vector images under a .png name."""
    try:
        from PIL import Image  # local import: Pillow is only needed here
        with Image.open(io.BytesIO(data)) as img:
            return (img.format or "").upper() or None
    except Exception:  # noqa: BLE001
        return None


def evaluate_image(data: bytes) -> ImageEval:
    """Classify raw image bytes as meaningful (worth describing) or decorative.

    Boilerplate (skins/ui-icons/spacers) is already filtered at download time, so
    this screens by size, pixel dimensions, and format. Non-raster (SVG/WMF),
    unsupported, or corrupt bytes can't be sent to the VLM → not meaningful."""
    sha = hashlib.sha256(data).hexdigest()
    if len(data) < settings.image_min_bytes:
        return ImageEval(False, None, None, sha)
    try:
        from PIL import Image  # local import: Pillow is only needed here
        with Image.open(io.BytesIO(data)) as img:
            w, h = img.size
            fmt = (img.format or "").upper()
    except Exception:  # noqa: BLE001 — unreadable/non-raster → not meaningful
        return ImageEval(False, None, None, sha)
    # WMF and friends report a header size via Image.open (lazy) but can't be
    # decoded or sent to the VLM — exclude by format, not just by dimensions.
    if fmt not in _VLM_IMAGE_FORMATS:
        return ImageEval(False, w, h, sha)
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


async def enrich_run_images(db: AsyncSession, source_id: uuid.UUID, run_id: uuid.UUID, *,
                            describe=None, max_new: int | None = None) -> int:
    """Enrichment phase: describe meaningful images of this source's articles, inject
    captions, and emit an 'updated' content_changes row per enriched article. Best-effort
    — never raises into extraction; leaves content_hash and versions untouched.

    Returns the number of images newly described this run (0 if disabled or on error)."""
    if not settings.image_vlm_enabled:
        return 0
    try:
        describe = describe or describe_image   # call-time lookup (honors monkeypatch)
        budget = settings.image_vlm_max_per_run if max_new is None else max_new
        described = 0
        consecutive_failures = 0
        media_root = os.path.abspath(settings.media_dir)

        # VLM describe calls are network-bound and slow; run them concurrently
        # (bounded). DB writes stay serialized on the single session below.
        sem = asyncio.Semaphore(max(1, settings.image_vlm_concurrency))

        async def _describe_bounded(data: bytes, alt: str | None, mime: str):
            async with sem:
                return await describe(data, alt, mime=mime)

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
            # (img, data, mime) for meaningful, uncached images needing a VLM call.
            pending: list[tuple[ArticleImage, bytes, str]] = []
            for img in imgs:
                path = os.path.join(media_root, str(art_id), img.local_filename)
                if not os.path.isfile(path):
                    # No bytes on disk → can't describe. Mark unevaluated ones
                    # not-meaningful so they drop out of the pending backlog instead of
                    # keeping the source perpetually "pending" and re-queuing no-op runs.
                    if img.is_meaningful is None:
                        img.is_meaningful = False
                    continue
                data = await asyncio.to_thread(_read_file, path)
                if img.is_meaningful is None:
                    ev = await asyncio.to_thread(evaluate_image, data)
                    img.is_meaningful = ev.is_meaningful
                    img.width, img.height, img.bytes_sha256 = ev.width, ev.height, ev.bytes_sha256
                if not img.is_meaningful:
                    continue
                # Cache hit → apply immediately (no VLM call). Otherwise queue the
                # image for a concurrent describe, reserving budget up front.
                cached = await db.get(ImageDescriptionCache, img.bytes_sha256)
                if cached is not None:
                    img.description, img.kind = cached.description, cached.kind or "other"
                    article.content_markdown = inject_caption(
                        article.content_markdown, img.local_path, cached.description)
                    changed = True
                    described += 1
                else:
                    # Gate on the real format (from bytes, not the .png name): a
                    # WMF/vector mislabeled .png 400s the VLM on every run. Mark it
                    # not-meaningful so it leaves the backlog for good.
                    fmt = _detect_format(data)
                    if fmt not in _VLM_IMAGE_FORMATS:
                        img.is_meaningful = False
                        continue
                    if budget <= 0 or consecutive_failures >= settings.image_vlm_max_consecutive_failures:
                        continue
                    budget -= 1  # reserve; refunded below if the call fails
                    pending.append((img, data, _VLM_IMAGE_FORMATS[fmt]))

            # Describe the queued images concurrently, then apply results serially.
            if pending:
                results = await asyncio.gather(
                    *[_describe_bounded(d, im.alt_text, m) for (im, d, m) in pending],
                    return_exceptions=True,
                )
                seen_sha: set[str] = set()
                for (img, _d, _m), res in zip(pending, results):
                    if isinstance(res, Exception) or res is None:
                        if isinstance(res, Exception):
                            logger.warning("describe_image failed: %s", res)
                        consecutive_failures += 1
                        budget += 1  # refund — nothing stored for this image
                        continue
                    consecutive_failures = 0
                    # One cache row per distinct image hash (an article may repeat
                    # an image) to avoid a duplicate-PK insert in this commit.
                    if img.bytes_sha256 not in seen_sha:
                        db.add(ImageDescriptionCache(
                            bytes_sha256=img.bytes_sha256, description=res.text,
                            kind=res.kind, model=settings.image_vlm_model,
                        ))
                        seen_sha.add(img.bytes_sha256)
                    img.description, img.kind = res.text, res.kind
                    article.content_markdown = inject_caption(
                        article.content_markdown, img.local_path, res.text)
                    changed = True
                    described += 1

            if changed:
                await change_log.record_change(db, article=article, change_type="updated", run_id=run_id)
            await db.commit()
            if consecutive_failures >= settings.image_vlm_max_consecutive_failures:
                break
        return described
    except Exception as exc:  # noqa: BLE001 — enrichment is best-effort
        logger.warning("enrich_run_images failed for source %s: %s", source_id, exc)
        # Roll back so the shared session (which extract_source keeps using to
        # finish the run) isn't left in a failed-transaction state — otherwise a
        # best-effort failure here would poison the completion path and fail the run.
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return 0


def _read_file(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()
