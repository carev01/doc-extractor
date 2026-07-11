"""VLM image-description enrichment.

evaluate_image  — dimension/size selection over raw bytes (sync; wrap in to_thread).
describe_image  — one OpenAI-compatible vision call → ImageDescription | None (Task 3).
inject_caption  — idempotent caption injection into markdown (Task 4).
enrich_run_images — the per-run enrichment phase (Task 5).
"""

import hashlib
import io
from dataclasses import dataclass

from app.core.config import settings


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
