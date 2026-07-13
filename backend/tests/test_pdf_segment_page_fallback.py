"""split_into_segments must not drop an outline entry whose section isn't
delimited in Docling's heading stream (outline finer-grained than detected
headings). Such an entry now falls back to its page-range text instead of an
empty body. Regression for the Dell-PDF "many TOC sections have no content" bug.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.pdf_convert import ConvertedDoc, split_into_segments
from app.services.pdf_import import Segment


def test_unmatched_outline_entry_gets_page_range_text():
    # Converted markdown has a heading for "Alpha" but NOT for "Beta"; Beta's
    # text lives only in page 2's text.
    lines = ["## Alpha", "alpha body text", "", ""]
    converted = ConvertedDoc(
        markdown="\n".join(lines),
        headings=[],
        page_texts=["## Alpha\nalpha body text", "Beta real content from page two"],
        table_pages=set(),
        page_line_starts=[0, 3],
    )
    outline = [
        Segment(title="Alpha", level=1, page_start=0, page_end=0, path=["Alpha"]),
        Segment(title="Beta", level=1, page_start=1, page_end=1, path=["Beta"]),
    ]

    segs = split_into_segments(converted, outline)
    by_title = {s.title: s for s in segs}

    assert "alpha body text" in by_title["Alpha"].markdown
    # Beta had no heading + an empty line-slice → must be filled from page 2 text,
    # not dropped as empty.
    assert by_title["Beta"].markdown.strip() != ""
    assert "Beta real content from page two" in by_title["Beta"].markdown
