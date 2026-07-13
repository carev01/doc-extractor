"""split_into_segments must only cut on outline entries that map to a heading
Docling actually detected. An entry with no matching heading is NOT turned into
a page-anchored fragment (that produced garbage, unrelated-content articles for
finer-grained outlines like Dell manuals) — its text stays within the preceding
matched section, keeping content correct. The converted markdown holds the whole
document's text, so nothing is lost, just grouped more coarsely.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.pdf_convert import ConvertedDoc, split_into_segments
from app.services.pdf_import import Segment


def test_unmatched_outline_entry_is_grouped_not_fragmented():
    # Docling emitted a heading for "Alpha" but not for "Beta"; both sections'
    # text is in the converted markdown (Docling converts the whole document).
    lines = ["## Alpha", "alpha body text", "", "beta body text", ""]
    converted = ConvertedDoc(
        markdown="\n".join(lines),
        headings=[],
        page_texts=["## Alpha\nalpha body text", "beta body text"],
        table_pages=set(),
        page_line_starts=[0, 3],
    )
    outline = [
        Segment(title="Alpha", level=1, page_start=0, page_end=0, path=["Alpha"]),
        Segment(title="Beta", level=1, page_start=1, page_end=1, path=["Beta"]),
    ]

    segs = split_into_segments(converted, outline)

    # Only the heading-matched entry becomes a segment — no page-anchored "Beta".
    assert [s.title for s in segs] == ["Alpha"]
    # Beta's text is preserved, grouped under Alpha's section (not lost).
    assert "alpha body text" in segs[0].markdown
    assert "beta body text" in segs[0].markdown


def test_matched_outline_entries_get_a_segment_each():
    # When every outline entry maps to a heading, each is its own segment with
    # its own body (the well-structured-PDF path, unaffected by the fix).
    lines = ["# One", "first body", "", "# Two", "second body", ""]
    converted = ConvertedDoc(
        markdown="\n".join(lines), headings=[],
        page_texts=["# One\nfirst body\n# Two\nsecond body"],
        table_pages=set(), page_line_starts=[0],
    )
    outline = [
        Segment(title="One", level=1, page_start=0, page_end=0, path=["One"]),
        Segment(title="Two", level=1, page_start=0, page_end=0, path=["Two"]),
    ]
    segs = split_into_segments(converted, outline)
    assert [s.title for s in segs] == ["One", "Two"]
    assert "first body" in segs[0].markdown and "second body" not in segs[0].markdown
    assert "second body" in segs[1].markdown


def test_bold_and_plain_title_lines_become_segments():
    # Docling emitted no ATX headings — section titles are a bold line and a bare
    # standalone line. Both outline entries must now map to their own segment with
    # their own body (accurate fine-grained TOC), not collapse to one.
    lines = [
        "**Getting started**", "",
        "install body text", "",
        "Configure backups", "",     # bare title line (no punctuation)
        "configure body text", "",
    ]
    converted = ConvertedDoc(
        markdown="\n".join(lines), headings=[],
        page_texts=["\n".join(lines)], table_pages=set(), page_line_starts=[0],
    )
    outline = [
        Segment(title="Getting started", level=1, page_start=0, page_end=0, path=["Getting started"]),
        Segment(title="Configure backups", level=1, page_start=0, page_end=0, path=["Configure backups"]),
    ]
    segs = split_into_segments(converted, outline)
    assert [s.title for s in segs] == ["Getting started", "Configure backups"]
    assert "install body text" in segs[0].markdown and "configure body text" not in segs[0].markdown
    assert "configure body text" in segs[1].markdown


def test_numbered_outline_matches_bold_title_by_containment():
    # Outline carries section numbering the rendered heading lacks; a *strong*
    # (bold) line still matches via containment.
    lines = ["**Overview**", "", "overview body", ""]
    converted = ConvertedDoc(
        markdown="\n".join(lines), headings=[],
        page_texts=["\n".join(lines)], table_pages=set(), page_line_starts=[0],
    )
    outline = [Segment(title="1.2 Overview", level=2, page_start=0, page_end=0, path=["1.2 Overview"])]
    segs = split_into_segments(converted, outline)
    assert [s.title for s in segs] == ["1.2 Overview"]
    assert "overview body" in segs[0].markdown


def test_bare_prose_line_is_not_matched_as_a_heading():
    # A body sentence must never be mistaken for a heading (weak lines match only
    # by exact equality). "Beta" has no title line, so it stays grouped.
    lines = ["## Alpha", "", "Click Save to apply the changes.", ""]
    converted = ConvertedDoc(
        markdown="\n".join(lines), headings=[],
        page_texts=["\n".join(lines)], table_pages=set(), page_line_starts=[0],
    )
    outline = [
        Segment(title="Alpha", level=1, page_start=0, page_end=0, path=["Alpha"]),
        Segment(title="Save", level=2, page_start=0, page_end=0, path=["Save"]),
    ]
    segs = split_into_segments(converted, outline)
    assert [s.title for s in segs] == ["Alpha"]           # "Save" not matched to the sentence
    assert "Click Save to apply" in segs[0].markdown
