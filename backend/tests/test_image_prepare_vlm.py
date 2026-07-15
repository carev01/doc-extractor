"""prepare_for_vlm: normalize raw image bytes into a VLM-acceptable payload.

The regression this guards: huge/animated documentation GIFs were marked
is_meaningful=True but 413/400-failed on every VLM call, so they stayed pending
forever. prepare_for_vlm must (a) pass small static images through untouched,
(b) collapse+downscale+re-encode animated/oversized images under the byte cap,
and (c) return None for bytes that can't be made describable so the caller drops
them from the backlog.
"""
import io
import os
import sys

from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.services.image_describe import prepare_for_vlm


def _png(w, h):
    buf = io.BytesIO()
    Image.effect_noise((w, h), 100).convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


def _animated_gif(w, h, frames=6):
    imgs = [Image.effect_noise((w, h), 100).convert("P") for _ in range(frames)]
    buf = io.BytesIO()
    imgs[0].save(buf, format="GIF", save_all=True, append_images=imgs[1:],
                 duration=80, loop=0)
    return buf.getvalue()


def test_small_static_png_passes_through_untouched():
    data = _png(300, 200)
    out = prepare_for_vlm(data)
    assert out is not None
    payload, mime = out
    assert payload is data          # same object → no re-encode
    assert mime == "image/png"


def test_large_animated_gif_becomes_small_jpeg_first_frame():
    # 3400x1400, several frames — the shape that produced 413s in production.
    data = _animated_gif(3400, 1400, frames=8)
    out = prepare_for_vlm(data)
    assert out is not None
    payload, mime = out
    assert mime == "image/jpeg"
    assert len(payload) <= settings.image_vlm_max_bytes
    with Image.open(io.BytesIO(payload)) as im:
        assert getattr(im, "n_frames", 1) == 1          # collapsed to one frame
        assert max(im.size) <= settings.image_vlm_max_dimension  # downscaled


def test_oversized_static_png_downscaled():
    data = _png(4000, 2000)
    out = prepare_for_vlm(data)
    assert out is not None
    payload, _mime = out
    with Image.open(io.BytesIO(payload)) as im:
        assert max(im.size) <= settings.image_vlm_max_dimension


def test_non_raster_bytes_return_none():
    assert prepare_for_vlm(b"this is not an image") is None


def test_returns_none_when_cannot_fit_cap(monkeypatch):
    # Force an unsatisfiable byte cap: even a downscaled JPEG can't fit, so the
    # image is unpreparable and must be dropped (None), not retried forever.
    monkeypatch.setattr(settings, "image_vlm_max_bytes", 200)
    assert prepare_for_vlm(_png(4000, 2000)) is None
