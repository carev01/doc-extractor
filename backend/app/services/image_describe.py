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
from xml.sax.saxutils import escape

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
# Anything else (WMF/EMF/TIFF/…) — even when mislabeled .png — is rejected by the
# VLM (400), so it is never worth describing. SVG is the exception: it is
# rasterized first (rasterize_svg), because GitBook/draw.io diagrams ship as SVG.
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


def _is_svg(data: bytes) -> bool:
    # Sniffed from content: the file name / Content-Type isn't reliable. It must
    # start as markup, so a raster whose metadata mentions "<svg" isn't one.
    head = data[:4096].lstrip(b"\xef\xbb\xbf \t\r\n")
    return head.startswith(b"<") and b"<svg" in head.lower()


# An HTML label neither SVG renderer draws: draw.io wraps each in a <switch> with
# an image fallback (which doesn't render either), so the whole switch is replaced.
_LABEL = re.compile(
    r"<switch\b[^>]*>(?:(?!</switch>).)*?<foreignObject\b.*?</switch>"
    r"|<foreignObject\b.*?</foreignObject>", re.S | re.I)
_FOREIGN = re.compile(r"<foreignObject\b([^>]*)>(.*?)</foreignObject>", re.S | re.I)


def _px(style: str, prop: str) -> float | None:
    m = re.search(rf"(?:^|[;\s]){prop}:\s*(-?[\d.]+)px", style)
    return float(m.group(1)) if m else None


def _labels_to_svg_text(svg: str) -> str:
    """Replace each HTML label (<foreignObject>) with SVG <text> at the label's
    position, so the renderer draws it. Veeam's draw.io diagrams carry *all* their text
    this way, and render as empty boxes without it. draw.io positions a label with
    its first div's ``margin-left``/``width``/``padding-top`` and
    ``justify-content``; any other foreignObject is centred in its own box."""
    def label(m: re.Match) -> str:
        fo = _FOREIGN.search(m.group(0))
        if fo is None:
            return m.group(0)
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(fo.group(2), "html.parser")
        for br in soup.find_all("br"):
            br.replace_with("\n")
        for block in soup.find_all(["div", "p", "li"]):
            block.append("\n")
        lines = [" ".join(ln.split()) for ln in soup.get_text().split("\n")]
        lines = [ln for ln in lines if ln]
        if not lines:
            return ""
        styles = [t.get("style", "") for t in soup.find_all(style=True)]
        box = styles[0] if styles else ""
        sizes = [v for v in (_px(st, "font-size") for st in styles) if v]
        size = sizes[-1] if sizes else 12.0
        x, w, y = _px(box, "margin-left"), _px(box, "width"), _px(box, "padding-top")
        if x is None or w is None or y is None:
            # Mermaid gives only width/height, inside a translated group.
            attrs = dict(re.findall(r'(\w+)="(-?[\d.]+)"', fo.group(1)))
            try:
                x, w = float(attrs.get("x", 0)), float(attrs["width"])
                y = float(attrs.get("y", 0)) + float(attrs["height"]) / 2
            except (KeyError, ValueError):
                return ""
        anchor, ax = "middle", x + w / 2
        if "flex-start" in box:
            anchor, ax = "start", x
        elif "flex-end" in box:
            anchor, ax = "end", x + w
        line_h = size * 1.2
        # Centre the block of lines vertically on y; +0.35em puts the baseline so
        # the glyphs, not their baseline, sit on the centre line.
        y0 = y - line_h * (len(lines) - 1) / 2 + size * 0.35
        spans = "".join(f'<tspan x="{ax:.1f}" y="{y0 + i * line_h:.1f}">{escape(ln)}</tspan>'
                        for i, ln in enumerate(lines))
        return (f'<text x="{ax:.1f}" y="{y0:.1f}" text-anchor="{anchor}" '
                f'font-family="Helvetica" font-size="{size:g}" fill="#333333">{spans}</text>')

    return _LABEL.sub(label, svg)


_SVG_ROOT = re.compile(r"<svg\b[^>]*>", re.I)


# CSS px per absolute unit (Inkscape writes mm).
_PX_PER = {"": 1.0, "px": 1.0, "pt": 4 / 3, "pc": 16.0, "in": 96.0, "cm": 96 / 2.54, "mm": 96 / 25.4}


def _svg_length(value: str | None) -> float | None:
    # An absolute length in CSS px; a relative one (%, em) has no size of its own.
    m = re.fullmatch(r"\s*([\d.]+)\s*(px|pt|pc|in|cm|mm)?\s*", value or "")
    if not m:
        return None
    return float(m.group(1)) * _PX_PER[m.group(2) or ""]


def _svg_size(svg: str) -> tuple[float, float] | None:
    """The SVG's own size in CSS px, from its root width/height, else its viewBox
    (Mermaid writes ``width="100%"``), or None if it declares neither."""
    root = _SVG_ROOT.search(svg)
    if root is None:
        return None
    attrs = dict(re.findall(r'([\w:-]+)\s*=\s*"([^"]*)"', root.group(0)))
    w, h = _svg_length(attrs.get("width")), _svg_length(attrs.get("height"))
    box = re.split(r"[\s,]+", attrs.get("viewBox", "").strip())
    if len(box) == 4:
        try:
            vw, vh = float(box[2]), float(box[3])
        except ValueError:
            vw = vh = 0.0
        if vw > 0 and vh > 0:
            if w and not h:
                h = w * vh / vw
            elif h and not w:
                w = h * vw / vh
            elif not w and not h:
                w, h = vw, vh
    return (w, h) if w and h else None


def _no_external_fetch(url: str, *args, **kwargs):
    # Vendor SVGs are untrusted: only inline data: URIs may be loaded — never a
    # URL (SSRF from inside the cluster) or a local file.
    if url.startswith("data:"):
        import weasyprint.urls
        return weasyprint.urls.default_url_fetcher(url, *args, **kwargs)
    raise ValueError("external resource blocked")


def rasterize_svg(data: bytes) -> bytes | None:
    """A PNG rendering of SVG *data* for the VLM, or None if *data* isn't an SVG
    that renders to something. WeasyPrint lays it out (it applies the SVG's
    <style> sheet, which MuPDF's own SVG reader ignores, filling every shape
    black) into a one-page PDF, and MuPDF rasterizes that page on white — the VLM
    sees a transparent background as black. Drawn at up to 2x (diagram text is
    small at 1x), with the long side within image_vlm_max_dimension and the pixel
    count within image_max_pixels. A blank rendering is None: it is what an SVG
    the renderers can't draw comes out as."""
    if not _is_svg(data):
        return None
    svg = data.decode("utf-8", "replace")
    if "<foreignobject" in svg.lower():
        svg = _labels_to_svg_text(svg)
    size = _svg_size(svg)
    if size is None or size[0] * size[1] > settings.image_max_pixels:
        return None
    w, h = size
    src = base64.b64encode(svg.encode()).decode()
    html = (f"<style>@page{{size:{w}px {h}px;margin:0}}html,body{{margin:0}}"
            f"img{{display:block;width:{w}px;height:{h}px}}</style>"
            f'<img src="data:image/svg+xml;base64,{src}">')
    try:
        import fitz  # local imports: only needed here
        import weasyprint
        pdf = weasyprint.HTML(string=html, url_fetcher=_no_external_fetch).write_pdf()
        with fitz.open(stream=pdf, filetype="pdf") as doc:
            # The PDF page is in points (0.75 per CSS px); MuPDF draws 1 px per point.
            zoom = min(2.0, settings.image_vlm_max_dimension / max(w, h)) / 0.75
            pix = doc[0].get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            return None if pix.is_unicolor else pix.tobytes("png")
    except Exception:  # noqa: BLE001 — malformed SVG → not renderable
        return None


def evaluate_image(data: bytes) -> ImageEval:
    """Classify raw image bytes as meaningful (worth describing) or decorative.

    Boilerplate (skins/ui-icons/spacers) is already filtered at download time, so
    this screens by size, pixel dimensions, and format. Non-raster (SVG/WMF),
    unsupported, or corrupt bytes can't be sent to the VLM → not meaningful."""
    sha = hashlib.sha256(data).hexdigest()
    if len(data) < settings.image_min_bytes:
        return ImageEval(False, None, None, sha)
    if _is_svg(data):
        # Judged at the SVG's own size (an icon stays an icon), and only if it
        # actually renders — otherwise it could never be described.
        if rasterize_svg(data) is None:
            return ImageEval(False, None, None, sha)
        w, h = (round(v) for v in _svg_size(data.decode("utf-8", "replace")))
        meaningful = w >= settings.image_min_dimension and h >= settings.image_min_dimension
        return ImageEval(meaningful, w, h, sha)
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


# OpenRouter answers a burst of concurrent describes with 429 and a slow upstream
# with 504. Each counted toward the enrich circuit breaker, so a drain-all run
# (thousands pending) ended after its first five-in-a-row burst while reporting
# COMPLETED. Retry these with backoff; only a call that is still failing after
# the retries reaches the breaker.
_TRANSIENT_STATUS = (429, 500, 502, 503, 504)
_BACKOFF = 2.0           # seconds; doubles each attempt (2, 4, 8, 16, ...)
_MAX_RETRY_AFTER = 60.0  # never sleep longer than this on a provider's Retry-After
_sleep = asyncio.sleep   # indirection so tests don't wait out the backoff


def _retry_delay(resp: "httpx.Response | None", attempt: int) -> float:
    try:
        return min(float(resp.headers["Retry-After"]), _MAX_RETRY_AFTER)
    except (AttributeError, KeyError, ValueError):
        return _BACKOFF * 2 ** attempt


def api_error_message(resp: "httpx.Response") -> str:
    """The provider's own error text from a failed chat-completions response."""
    try:
        msg = resp.json()["error"]["message"]
    except Exception:  # noqa: BLE001 — not the OpenAI error shape
        msg = resp.text
    return " ".join(str(msg).split())[:300]


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
        "reasoning": {"enabled": settings.image_vlm_reasoning},
    }
    headers = {"Authorization": f"Bearer {settings.image_vlm_api_key}", "content-type": "application/json"}
    own = client is None
    c = client or httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))
    try:
        for attempt in range(settings.image_vlm_retries + 1):
            try:
                resp = await c.post(settings.image_vlm_base_url, headers=headers, json=body)
            except httpx.TransportError as exc:  # timeouts, dropped connections
                if attempt >= settings.image_vlm_retries:
                    raise
                logger.info("describe_image: %s, retrying", exc.__class__.__name__)
                await _sleep(_retry_delay(None, attempt))
                continue
            if resp.status_code not in _TRANSIENT_STATUS or attempt >= settings.image_vlm_retries:
                break
            delay = _retry_delay(resp, attempt)
            logger.info("describe_image: HTTP %d from %s, retrying in %.0fs",
                        resp.status_code, settings.image_vlm_model, delay)
            await _sleep(delay)
        if resp.is_error:
            # The status alone hides the cause: OpenRouter answers a retired model, a
            # model the account's guardrail blocks, and a bad route all with 404.
            logger.warning("describe_image failed: HTTP %d from %s: %s", resp.status_code,
                           settings.image_vlm_model, api_error_message(resp))
            return None
        choice = resp.json()["choices"][0]
        content = choice["message"].get("content") or ""
        if choice.get("finish_reason") == "length":
            logger.warning("describe_image: %s hit max_tokens=%d before finishing (reasoning on?)",
                           settings.image_vlm_model, settings.image_vlm_max_tokens)
        obj = json.loads(content)
        text = (obj.get("description") or "").strip()
        if not text:
            logger.warning("describe_image failed: empty description from %s (finish_reason=%s)",
                           settings.image_vlm_model, choice.get("finish_reason"))
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
# The quoted alternatives allow escaped-quote runs (\" / \'), not just "no quote at
# all": an HTML title itself containing a double quote (e.g. an alt/title copied
# from quoted UI text) comes through markdownify as \" and a bare [^"]* stops at
# the first one, well short of the real closing quote — same silent-skip failure
# as an unhandled title form.
_MD_TITLE = r"(?:\s+(?:\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|\([^)]*\)))?"


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
                    if _is_svg(data):
                        data = await asyncio.to_thread(rasterize_svg, data) or b""
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
                logger.warning("enrich: stopping after %d consecutive VLM failures; the rest stay "
                               "pending for the next run", consecutive_failures)
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
