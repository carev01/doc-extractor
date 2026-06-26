import os
import sys

import fitz

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.pdf_import import segment_pdf


def _pdf_with_big_headings() -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Getting Started", fontsize=24)
    page.insert_text((72, 110), "Some body copy explaining things.", fontsize=11)
    page.insert_text((72, 200), "Advanced Usage", fontsize=24)
    page.insert_text((72, 238), "More body copy here.", fontsize=11)
    return doc.tobytes()


def test_heuristic_splits_on_large_headings_when_no_outline():
    segs = segment_pdf(_pdf_with_big_headings())
    titles = [s.title for s in segs]
    assert "Getting Started" in titles
    assert "Advanced Usage" in titles
    assert len(segs) == 2
