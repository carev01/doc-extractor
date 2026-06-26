import os
import sys

import fitz

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.pdf_import import segment_to_markdown, render_segments, Segment


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


def test_render_segments_matches_per_segment_and_preserves_order():
    """Batch rendering (single PDF open) returns the same per-segment markdown,
    in order, as calling segment_to_markdown on each segment individually."""
    pdf = _pdf()
    segs = [
        Segment("Alpha", 1, 0, 0, ["Alpha"]),
        Segment("Beta", 1, 1, 1, ["Beta"]),
    ]
    batch = render_segments(pdf, segs)
    assert batch == [segment_to_markdown(pdf, s) for s in segs]
    assert "Alpha section content" in batch[0]
    assert "Beta section content" in batch[1]
