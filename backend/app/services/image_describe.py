"""VLM image-description enrichment.

evaluate_image  — dimension/size selection over raw bytes (sync; wrap in to_thread).
describe_image  — one OpenAI-compatible vision call → ImageDescription | None (Task 3).
inject_caption  — idempotent caption injection into markdown (Task 4).
enrich_run_images — the per-run enrichment phase (Task 5).
"""

import base64
import hashlib
import io
import json
import logging
from dataclasses import dataclass

import httpx

from app.core.config import settings

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
