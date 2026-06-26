import hashlib
import os
import re
import sys

import fitz

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.pdf_import import (
    Segment, RenderedImage, render_segments, segment_to_markdown,
)


def _img_pixmap(rgb=(255, 0, 0)):
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 64, 64))
    pix.set_rect(pix.irect, rgb)
    return pix


def _pdf_one_image() -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Heading above the figure")
    page.insert_image(fitz.Rect(72, 100, 200, 200), pixmap=_img_pixmap())
    page.insert_text((72, 320), "Caption below the figure")
    return doc.tobytes()


def _pdf_no_image() -> bytes:
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "Just text, no figures.")
    return doc.tobytes()


def test_image_is_content_addressed_and_referenced():
    pdf = _pdf_one_image()
    seg = Segment("Doc", 1, 0, 0, ["Doc"])
    [(md, images)] = render_segments(pdf, [seg])
    assert len(images) == 1
    img = images[0]
    # filename is sha256(bytes)[:16] + .png
    assert img.filename == hashlib.sha256(img.data).hexdigest()[:16] + ".png"
    # markdown references the bare canonical filename (no temp path, no /media)
    assert f"]({img.filename})" in md
    assert "/tmp" not in md and "/media" not in md
    # surrounding text preserved
    assert "Heading above the figure" in md and "Caption below the figure" in md


def test_no_image_segment_yields_no_rendered_images():
    pdf = _pdf_no_image()
    seg = Segment("Doc", 1, 0, 0, ["Doc"])
    [(md, images)] = render_segments(pdf, [seg])
    assert images == []
    assert "![" not in md


def _pdf_two_identical_images() -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 60), "Top")
    page.insert_image(fitz.Rect(72, 80, 180, 180), pixmap=_img_pixmap())
    page.insert_text((72, 200), "Middle")
    page.insert_image(fitz.Rect(72, 220, 180, 320), pixmap=_img_pixmap())
    return doc.tobytes()


def test_identical_images_dedupe_to_one_rendered_image():
    # Two placements of the same image bytes collapse to a single RenderedImage
    # (content-addressed), regardless of how many markers pymupdf4llm emits.
    pdf = _pdf_two_identical_images()
    seg = Segment("Doc", 1, 0, 0, ["Doc"])
    [(md, images)] = render_segments(pdf, [seg])
    assert len(images) == 1                               # same bytes → one image
    assert f"]({images[0].filename})" in md              # referenced by canonical name


def test_segment_to_markdown_still_returns_str():
    pdf = _pdf_one_image()
    md = segment_to_markdown(pdf, Segment("Doc", 1, 0, 0, ["Doc"]))
    assert isinstance(md, str)
    assert "Heading above the figure" in md
