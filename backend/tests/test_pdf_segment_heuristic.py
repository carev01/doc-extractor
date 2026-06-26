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


def _pdf_headings_across_pages() -> bytes:
    """Two headings on separate pages, with a third body-only page after the
    second heading — exercises the multi-page boundary computation."""
    doc = fitz.open()
    p0 = doc.new_page()
    p0.insert_text((72, 72), "Chapter One", fontsize=24)
    p0.insert_text((72, 110), "Body of chapter one.", fontsize=11)
    p1 = doc.new_page()
    p1.insert_text((72, 72), "Chapter Two", fontsize=24)
    p1.insert_text((72, 110), "Body of chapter two.", fontsize=11)
    doc.new_page().insert_text((72, 72), "Continued body of chapter two.", fontsize=11)
    return doc.tobytes()


def test_heuristic_segments_are_ordered_and_non_overlapping():
    segs = segment_pdf(_pdf_headings_across_pages())
    assert [s.title for s in segs] == ["Chapter One", "Chapter Two"]
    # Chapter One: page 0 only; Chapter Two: pages 1-2 (its body spills to page 2).
    assert (segs[0].page_start, segs[0].page_end) == (0, 0)
    assert (segs[1].page_start, segs[1].page_end) == (1, 2)
    # No page belongs to two segments, and ranges are in order.
    assert segs[0].page_end < segs[1].page_start
