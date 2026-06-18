import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.pdf_renderer import render_markdown_to_pdf


def test_renders_pdf_bytes():
    pdf = render_markdown_to_pdf("# Title\n\nSome **bold** text.", base_url="/tmp/")
    assert pdf[:5] == b"%PDF-"
    assert len(pdf) > 500


def test_renders_tables_and_code():
    md = "| a | b |\n|---|---|\n| 1 | 2 |\n\n```\ncode\n```\n"
    pdf = render_markdown_to_pdf(md, base_url="/tmp/")
    assert pdf[:5] == b"%PDF-"


def test_embeds_local_image(tmp_path):
    # A 1x1 PNG written to base_url; referencing it should grow the PDF.
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108020000"
        "00907753de0000000c4944415408d763f8cfc0f01f0005000155a3"
        "0a0a0000000049454e44ae426082"
    )
    (tmp_path / "img.png").write_bytes(png)
    base = str(tmp_path) + os.sep
    without = render_markdown_to_pdf("# No image", base_url=base)
    with_img = render_markdown_to_pdf("# Image\n\n![x](img.png)", base_url=base)
    assert with_img[:5] == b"%PDF-"
    assert len(with_img) > len(without)


def test_missing_image_does_not_raise(tmp_path):
    base = str(tmp_path) + os.sep
    pdf = render_markdown_to_pdf("# Doc\n\n![gone](does-not-exist.png)", base_url=base)
    assert pdf[:5] == b"%PDF-"
