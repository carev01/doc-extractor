"""evaluate_image: dimension/size selection over raw image bytes."""
import io
import os
import sys

from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
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
    # Byte-heavy (noise) so it passes the size threshold and actually reaches the
    # dimension check — isolating the w/h < image_min_dimension rejection branch.
    data = _noise_png(40, 40)
    assert len(data) >= 3072
    ev = evaluate_image(data)
    assert ev.is_meaningful is False
    assert ev.width == 40 and ev.height == 40  # dims were read, then rejected on size


def test_sub_min_bytes_rejected():
    # A 1x1 PNG is well under image_min_bytes.
    data = _png(1, 1)
    ev = evaluate_image(data)
    assert ev.is_meaningful is False


def test_non_raster_bytes_rejected():
    # Unparsable SVG (concatenated documents) / corrupt bytes: neither Pillow nor
    # the SVG renderer can open it → not meaningful, dims None, hash set.
    data = b"<svg xmlns='http://www.w3.org/2000/svg'></svg>" * 100
    ev = evaluate_image(data)
    assert ev.is_meaningful is False
    assert ev.width is None and ev.height is None
    assert len(ev.bytes_sha256) == 64


def test_bomb_scale_pixels_rejected(monkeypatch):
    # An image whose pixel count exceeds image_max_pixels must be rejected without
    # a full decode — decoding one 129 MP image OOM-killed the worker. Uses a small
    # cap + modest image so the test doesn't allocate the real bomb.
    monkeypatch.setattr(settings, "image_max_pixels", 1000)  # cap at 1000 px
    data = _noise_png(400, 300)  # 120,000 px > cap
    ev = evaluate_image(data)
    assert ev.is_meaningful is False
    assert ev.width == 400 and ev.height == 300  # dims read from header, then rejected


def test_hash_is_stable_and_content_addressed():
    a = _png(400, 300, (10, 20, 30))
    assert evaluate_image(a).bytes_sha256 == evaluate_image(a).bytes_sha256
    b = _png(400, 300, (200, 100, 50))
    assert evaluate_image(a).bytes_sha256 != evaluate_image(b).bytes_sha256


def _svg(w, h, boxes=40):
    # A draw.io-style diagram: labelled boxes, enough markup to clear image_min_bytes.
    body = "".join(
        f'<rect x="{10 + (i % 8) * 100}" y="{10 + (i // 8) * 80}" width="90" height="60" '
        f'fill="#e3f2ef" stroke="#2a8a7a"/><text x="{20 + (i % 8) * 100}" y="{45 + (i // 8) * 80}" '
        f'font-family="Helvetica" font-size="12px">Box {i}</text>'
        for i in range(boxes))
    return (f'<?xml version="1.0" encoding="utf-8"?>\n<svg xmlns="http://www.w3.org/2000/svg" '
            f'width="{w}px" height="{h}px" viewBox="0 0 {w} {h}">{body}</svg>').encode()


def test_svg_diagram_is_meaningful_at_its_own_size():
    import hashlib
    data = _svg(871, 446)
    assert len(data) >= 3072
    ev = evaluate_image(data)
    assert ev.is_meaningful is True
    # Dimensions are the SVG's own, not the (upscaled) rendering's.
    assert (ev.width, ev.height) == (871, 446)
    # Hashed on the stored SVG bytes, so the description cache keys on the file.
    assert ev.bytes_sha256 == hashlib.sha256(data).hexdigest()


def test_small_svg_icon_rejected():
    data = _svg(24, 24, boxes=40)
    assert len(data) >= 3072
    ev = evaluate_image(data)
    assert ev.is_meaningful is False
    assert (ev.width, ev.height) == (24, 24)


# A draw.io label: HTML inside <foreignObject>, which MuPDF can't draw, with an
# image fallback in the <switch> that it doesn't draw either.
_DRAWIO_LABEL = (
    '<switch><foreignObject style="overflow: visible;" pointer-events="none" width="100%" '
    'height="100%" requiredFeatures="http://www.w3.org/TR/SVG11/feature#Extensibility">'
    '<div xmlns="http://www.w3.org/1999/xhtml" style="display: flex; align-items: unsafe center; '
    'justify-content: unsafe center; width: 118px; height: 1px; padding-top: 52px; margin-left: 60px;">'
    '<div style="box-sizing: border-box; font-size: 0; text-align: center;">'
    '<div style="display: inline-block; font-size: 10px;"><span style="font-size: 12px;">'
    '<font>Backup</font> server<br>Proxy &amp; repo</span></div></div></div></foreignObject>'
    '<image x="60" y="45" width="118" height="17" xlink:href="data:image/png;base64,AAAA"/></switch>')


def _drawio_svg(labels=1):
    return ('<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
            'width="240px" height="140px" viewBox="0 0 240 140"><!-- ' + "x" * 3000 + ' -->'
            + _DRAWIO_LABEL * labels + '</svg>').encode()


def test_drawio_html_labels_become_svg_text():
    from app.services.image_describe import _labels_to_svg_text
    out = _labels_to_svg_text(_drawio_svg().decode())
    assert "<foreignObject" not in out and "<switch" not in out and "<image" not in out
    # Centred on the label box (margin-left + width/2, padding-top), one line per <br>.
    assert '<text x="119.0"' in out and 'text-anchor="middle"' in out and 'font-size="12"' in out
    assert ">Backup server</tspan>" in out and ">Proxy &amp; repo</tspan>" in out


def test_svg_whose_only_ink_is_html_labels_still_renders():
    # Without the conversion the rendering is blank, so the diagram is dropped.
    ev = evaluate_image(_drawio_svg())
    assert ev.is_meaningful is True and (ev.width, ev.height) == (240, 140)


def _pixel(png, x, y):
    with Image.open(io.BytesIO(png)) as im:
        return im.convert("RGB").getpixel((x, y))


def test_svg_stylesheet_fills_are_applied():
    # IBM/N-Able/Rubrik diagrams colour shapes through a <style> sheet; a renderer
    # that ignores it fills every shape black and the VLM describes black boxes.
    from app.services.image_describe import rasterize_svg
    data = ('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100" viewBox="0 0 200 100">'
            '<style>.box{fill:#ffffff;stroke:#3355aa;stroke-width:2px}</style><!-- ' + "x" * 3000 + ' -->'
            '<rect class="box" x="10" y="10" width="180" height="80"/></svg>').encode()
    png = rasterize_svg(data)
    with Image.open(io.BytesIO(png)) as im:
        w, h = im.size
    assert _pixel(png, w // 2, h // 2) == (255, 255, 255)


def test_svg_sized_by_viewbox_when_width_is_relative():
    # Mermaid writes width="100%" and carries its size only in the viewBox.
    data = ('<svg xmlns="http://www.w3.org/2000/svg" width="100%" viewBox="0 0 696 1308">'
            '<!-- ' + "x" * 3000 + ' --><rect x="10" y="10" width="300" height="300" fill="#226"/></svg>').encode()
    ev = evaluate_image(data)
    assert ev.is_meaningful is True and (ev.width, ev.height) == (696, 1308)


def test_mermaid_html_labels_become_svg_text():
    # Mermaid's labels: a foreignObject with only width/height, inside a
    # translated group (x/y default to 0).
    from app.services.image_describe import _labels_to_svg_text
    svg = ('<g class="label" transform="translate(-40, -12)"><foreignObject width="80" height="24">'
           '<div xmlns="http://www.w3.org/1999/xhtml" style="display: table-cell;">'
           '<span class="nodeLabel"><p>Run backup</p></span></div></foreignObject></g>')
    out = _labels_to_svg_text(svg)
    assert '<text x="40.0"' in out and ">Run backup</tspan>" in out


def test_svg_external_references_are_never_fetched(monkeypatch):
    # Vendor SVGs are untrusted: an external <image href> must not make the worker
    # fetch anything (SSRF / local-file reads); data: URIs still render.
    import weasyprint.urls
    from app.services.image_describe import rasterize_svg
    fetched = []
    real = weasyprint.urls.default_url_fetcher
    monkeypatch.setattr(weasyprint.urls, "default_url_fetcher",
                        lambda url, *a, **k: fetched.append(url) or real(url, *a, **k))
    data = ('<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
            'width="200" height="100"><!-- ' + "x" * 3000 + ' -->'
            '<image href="http://169.254.169.254/latest" width="50" height="50"/>'
            '<image xlink:href="file:///etc/passwd" width="50" height="50"/>'
            '<rect x="10" y="10" width="50" height="50" fill="#a00"/></svg>').encode()
    assert rasterize_svg(data) is not None
    assert not [u for u in fetched if not u.startswith("data:")]


def test_svg_size_in_absolute_units():
    # Inkscape writes millimetres (Trilio): 86.8mm x 64.6mm is ~328 x 244 px, not
    # the 87 x 65 of its viewBox.
    from app.services.image_describe import _svg_size
    svg = '<svg xmlns="http://www.w3.org/2000/svg" width="86.811325mm" height="64.600525mm" viewBox="0 0 86.8 64.6">'
    w, h = _svg_size(svg)
    assert (round(w), round(h)) == (328, 244)
    assert _svg_size('<svg width="1in" height="2cm">') == (96.0, 2 * 96 / 2.54)


def test_raster_mentioning_svg_in_metadata_is_not_an_svg():
    from PIL.PngImagePlugin import PngInfo
    meta = PngInfo(); meta.add_text("Comment", "<svg> exported")
    buf = io.BytesIO()
    Image.effect_noise((400, 300), 100).convert("RGB").save(buf, format="PNG", pnginfo=meta)
    ev = evaluate_image(buf.getvalue())
    assert ev.is_meaningful is True and (ev.width, ev.height) == (400, 300)
