import os
import sys

import fitz

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.pdf_import import segment_to_markdown, Segment


def _pdf() -> bytes:
    doc = fitz.open()
    p0 = doc.new_page(); p0.insert_text((72, 72), "Alpha section content")
    p1 = doc.new_page(); p1.insert_text((72, 72), "Beta section content")
    return doc.tobytes()


def test_renders_only_segment_pages():
    pdf = _pdf()
    md = segment_to_markdown(pdf, Segment("Alpha", 1, 0, 0, ["Alpha"]))
    assert "Alpha section content" in md
    assert "Beta section content" not in md
    assert md.strip()
