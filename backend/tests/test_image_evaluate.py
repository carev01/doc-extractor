"""evaluate_image: dimension/size selection over raw image bytes."""
import io
import os
import sys

from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.image_describe import evaluate_image


def _png(w, h, color=(120, 130, 140)):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


def _noise_png(w, h):
    # A noise image doesn't compress, so it exceeds image_min_bytes — a realistic
    # stand-in for a screenshot/diagram (a solid-color PNG compresses to ~1 KB,
    # unrealistically small, so it must not be used for the "meaningful" case).
    buf = io.BytesIO()
    Image.effect_noise((w, h), 100).convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


def test_large_image_is_meaningful():
    data = _noise_png(400, 300)
    assert len(data) >= 3072  # genuinely above the byte threshold
    ev = evaluate_image(data)
    assert ev.is_meaningful is True
    assert ev.width == 400 and ev.height == 300
    assert len(ev.bytes_sha256) == 64


def test_tiny_dimension_rejected():
    data = _png(40, 40)
    ev = evaluate_image(data)
    assert ev.is_meaningful is False


def test_sub_min_bytes_rejected():
    # A 1x1 PNG is well under image_min_bytes.
    data = _png(1, 1)
    ev = evaluate_image(data)
    assert ev.is_meaningful is False


def test_non_raster_bytes_rejected():
    # SVG / corrupt bytes: Pillow can't open → not meaningful, dims None, hash set.
    data = b"<svg xmlns='http://www.w3.org/2000/svg'></svg>" * 100
    ev = evaluate_image(data)
    assert ev.is_meaningful is False
    assert ev.width is None and ev.height is None
    assert len(ev.bytes_sha256) == 64


def test_hash_is_stable_and_content_addressed():
    a = _png(400, 300, (10, 20, 30))
    assert evaluate_image(a).bytes_sha256 == evaluate_image(a).bytes_sha256
    b = _png(400, 300, (200, 100, 50))
    assert evaluate_image(a).bytes_sha256 != evaluate_image(b).bytes_sha256
