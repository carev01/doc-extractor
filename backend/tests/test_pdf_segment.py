import os
import sys

import fitz  # PyMuPDF

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.services.pdf_import as pi
from app.services.pdf_import import Segment


def _pdf_with_outline() -> bytes:
    doc = fitz.open()
    for i in range(4):
        page = doc.new_page()
        page.insert_text((72, 72), f"Body text page {i}")
    # [level, title, 1-based page]
    doc.set_toc([
        [1, "Chapter 1", 1],
        [2, "Installation", 2],
        [1, "Chapter 2", 3],
    ])
    return doc.tobytes()


def _pdf_no_outline() -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Just some text, no bookmarks.")
    return doc.tobytes()


def test_outline_segments_have_correct_ranges_levels_paths():
    segs = pi._outline_for(_pdf_with_outline())
    assert [s.title for s in segs] == ["Chapter 1", "Installation", "Chapter 2"]
    assert [s.level for s in segs] == [1, 2, 1]
    # Chapter 1: page 0; Installation: page 1; Chapter 2: pages 2-3
    assert (segs[0].page_start, segs[0].page_end) == (0, 0)
    assert (segs[1].page_start, segs[1].page_end) == (1, 1)
    assert (segs[2].page_start, segs[2].page_end) == (2, 3)
    # Path includes ancestors
    assert segs[1].path == ["Chapter 1", "Installation"]
    assert segs[2].path == ["Chapter 2"]


def test_no_outline_returns_empty_list():
    segs = pi._outline_for(_pdf_no_outline())
    assert segs == []
