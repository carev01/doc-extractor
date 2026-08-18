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
import re
import uuid
from dataclasses import dataclass

import httpx
from sqlalchemy import func, or_, select
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
    # Refuse to treat an absurdly large image as meaningful: fully decoding one
    # can allocate hundreds of MB (w*h*3 bytes) and OOM-kill the worker. .size is
    # read from the header, so this check happens before any decode.
    if w * h > settings.image_max_pixels:
        return ImageEval(False, w, h, sha)
    meaningful = w >= settings.image_min_dimension and h >= settings.image_min_dimension
    return ImageEval(meaningful, w, h, sha)


def prepare_for_vlm(data: bytes) -> "tuple[bytes, str] | None":
    """Normalize raw image bytes into a (payload, mime) pair a vision model can
    actually accept, or None if the image can't be made describable.

    Guards against the failures that otherwise keep an image pending forever:
    huge or animated GIFs (documentation screencasts) that the VLM rejects with
    413 Payload Too Large / 400 Bad Request on every run. An animated image is
    collapsed to its first frame, oversized images are downscaled to
    image_vlm_max_dimension, and the result is re-encoded so the payload stays
    under image_vlm_max_bytes. A small, static, correctly-formatted image passes
    through untouched (no re-encode). Returns None for non-raster/unreadable
    bytes or an image that can't be shrunk under the cap — the caller marks those
    not-meaningful so they leave the backlog for good."""
    fmt = _detect_format(data)
    if fmt not in _VLM_IMAGE_FORMATS:
        return None
    try:
        from PIL import Image  # local import: Pillow is only needed here
        with Image.open(io.BytesIO(data)) as img:
            animated = getattr(img, "n_frames", 1) > 1
            w, h = img.size
            # Never decode a bomb-scale image (defense for images marked meaningful
            # before the evaluate_image pixel guard existed): ~400 MB for one 129 MP
            # frame. It can't be usefully described anyway → drop from the backlog.
            if w * h > settings.image_max_pixels:
                return None
            within = max(w, h) <= settings.image_vlm_max_dimension
            # Fast path: static, in-bounds, already small enough — send as-is.
            if not animated and within and len(data) <= settings.image_vlm_max_bytes:
                return data, _VLM_IMAGE_FORMATS[fmt]
            if animated:
                img.seek(0)  # describe the first frame of a screencast/animation
            frame = img.convert("RGB")
        # Downscale + re-encode (JPEG) until the payload fits the byte cap.
        for max_dim, quality in (
            (settings.image_vlm_max_dimension, 85),
            (settings.image_vlm_max_dimension, 70),
            (1024, 70),
        ):
            im = frame.copy()
            im.thumbnail((max_dim, max_dim))
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=quality)
            out = buf.getvalue()
            if len(out) <= settings.image_vlm_max_bytes:
                return out, "image/jpeg"
        return None
    except Exception:  # noqa: BLE001 — unpreparable → drop from backlog
        return None


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


# Markdown title on an image reference: markdownify renders <img title="…"> as
# ![alt](src "title"), and some vendors (AvePoint, Securiti, Veeam Help Center)
# set title on every screenshot. The title may also be '…' or (…) quoted, and the
# URL itself may be wrapped in <>. Matching only "](url)" silently skipped every
# such image — the description was stored and counted as done, but no caption ever
# reached the content.
_MD_TITLE = r"(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^)]*\)))?"


def _link_target_re(image_url: str) -> str:
    """Regex source for ``](image_url "optional title")`` — the reference tail
    shared by an image token and a bare link. Anchored on the closing paren so a
    different image whose path merely *starts* with this one can't match."""
    return r"\]\(\s*<?" + re.escape(image_url) + r">?" + _MD_TITLE + r"\s*\)"


def _image_token_re(image_url: str) -> "re.Pattern[str]":
    """Regex for a whole ``![alt](image_url "optional title")`` token."""
    return re.compile(r"!\[[^\]]*" + _link_target_re(image_url))


def inject_caption(markdown: str, image_url: str, description: str) -> str:
    """Insert (or replace) a '> **Figure:** …' caption immediately below the first
    markdown image reference to *image_url*.

    The HTML→markdown conversion often leaves an image *inline* in a paragraph —
    its line carries prose before and after the ``![](…)`` token. Inserting after
    such a line drops the caption below the whole paragraph (visibly separated
    from the image by the trailing text). So an inline image is first isolated
    onto its own line, splitting the surrounding prose into its own paragraphs,
    and the caption is placed directly under the image. Idempotent for a given
    (image, text): a re-run sees the already-isolated image and just refreshes
    the caption."""
    lines = markdown.split("\n")
    tok_re = _image_token_re(image_url)
    match = None
    for i, ln in enumerate(lines):
        match = tok_re.search(ln)
        if match:
            idx = i
            break
    if match is None:
        # The URL is a reference target but not an image token (e.g. a bare link) —
        # keep the whole line and just place the caption after it.
        link_re = re.compile(_link_target_re(image_url))
        idx = next((i for i, ln in enumerate(lines) if link_re.search(ln)), None)
        if idx is None:
            return markdown

    block = _caption_block(description)
    line = lines[idx]
    if match is None:
        before, image_tok, after = "", line, ""
    else:
        start, end = match.span()
        before = line[:start].rstrip()
        image_tok = line[start:end]
        after = line[end:].strip()

    # Image already alone on its line: refresh an existing caption run, else add.
    if not before and not after:
        j = idx + 1
        if j < len(lines) and lines[j].strip() == "":
            j += 1
        if j < len(lines) and lines[j].lstrip().startswith("> **Figure:**"):
            end = j
            while end < len(lines) and lines[end].lstrip().startswith(">"):
                end += 1
            lines[idx:end] = [image_tok, "", block]
        else:
            lines[idx:idx + 1] = [image_tok, "", block]
        return "\n".join(lines)

    # Inline image: isolate it (with its caption) between the surrounding prose.
    rebuilt: list[str] = []
    if before:
        rebuilt += [before, ""]
    rebuilt += [image_tok, "", block]
    if after:
        rebuilt += ["", after]
    lines[idx:idx + 1] = rebuilt
    return "\n".join(lines)


async def repair_missing_captions(
    db: AsyncSession, source_id: uuid.UUID, run_id: uuid.UUID
) -> int:
    """Re-inject captions for already-described images whose caption never made it
    into the markdown. Returns the number of articles repaired.

    Needed because the describe pass only revisits images with no description: an
    image that was described while inject_caption still failed to match its
    reference (see _MD_TITLE) would keep its stored description — and stay counted
    as done — with no caption in the content, forever. Descriptions are reused from
    the rows themselves, so this costs no VLM calls.

    Scoped in SQL to articles holding a *titled* image reference, the only form the
    old matcher missed, so a healthy source loads nothing. Those articles are
    re-scanned on every run even once repaired (the title stays in the reference),
    but injection is idempotent: the markdown comes back identical, so there is no
    write and no outbox row."""
    titled_ref = Article.content_markdown.like(
        func.concat("%](", ArticleImage.local_path, " %")
    )
    art_ids = (await db.execute(
        select(Article.id).distinct()
        .join(ArticleImage, ArticleImage.article_id == Article.id)
        .where(
            Article.source_id == source_id,
            Article.removed_at.is_(None),
            ArticleImage.description.is_not(None),
            titled_ref,
        )
    )).scalars().all()

    repaired = 0
    for art_id in art_ids:
        article = await db.get(Article, art_id)
        if article is None:
            continue
        imgs = (await db.execute(
            select(ArticleImage)
            .where(ArticleImage.article_id == art_id, ArticleImage.description.is_not(None))
            .order_by(ArticleImage.sort_order)
        )).scalars().all()
        markdown = article.content_markdown
        for img in imgs:
            markdown = inject_caption(markdown, img.local_path, img.description)
        if markdown != article.content_markdown:
            article.content_markdown = markdown
            await change_log.record_change(
                db, article=article, change_type="updated", run_id=run_id)
            repaired += 1
        await db.commit()
    if repaired:
        logger.info(
            "re-injected missing image captions into %d article(s) of source %s",
            repaired, source_id,
        )
    return repaired


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

        # First heal any described-but-uncaptioned images (free — no VLM calls),
        # before the budget/circuit-breaker can cut this phase short.
        await repair_missing_captions(db, source_id, run_id)

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
                    # Cheap format gate first (reads only the header): a WMF/vector
                    # mislabeled .png can never be described — drop it now, regardless
                    # of budget, so it leaves the backlog for good.
                    if _detect_format(data) not in _VLM_IMAGE_FORMATS:
                        img.is_meaningful = False
                        continue
                    # Stop before the expensive step once budget/circuit-breaker is
                    # spent, so a large backlog doesn't re-encode thousands of images
                    # per run only to discard all but the budgeted few.
                    if budget <= 0 or consecutive_failures >= settings.image_vlm_max_consecutive_failures:
                        continue
                    # Normalize the bytes into something the VLM accepts (first frame
                    # of an animated GIF, downscaled/re-encoded under the payload cap).
                    # None → a huge GIF that can't be shrunk under the cap; it would
                    # 413 on every run, so mark it not-meaningful to leave the backlog.
                    prepared = await asyncio.to_thread(prepare_for_vlm, data)
                    if prepared is None:
                        img.is_meaningful = False
                        continue
                    budget -= 1  # reserve; refunded below if the call fails
                    pending.append((img, *prepared))

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
