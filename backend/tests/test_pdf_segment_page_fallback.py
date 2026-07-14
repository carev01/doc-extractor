"""split_into_segments is PAGE-ANCHORED: the PDF outline's bookmark page numbers
are authoritative (Docling's heading detection is not), so every outline entry
becomes an article at its bookmark page — refined to an exact/near heading when
Docling emitted one, else anchored at the page top. This gives finer-grained
outlines (Dell manuals) their real content per TOC item instead of dropping
unmatched entries and absorbing their text into a neighbour. Two guards keep it
from fragmenting into garbage: (1) an entry sharing a page with an already-emitted
entry, and lacking a heading, is grouped (not split by a 1-line guess); (2) a
title matches a heading only by exact equality or SAFE containment (heading ⊆
title, for section numbering) — never title ⊆ heading, which used to mis-match a
longer sub-heading.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.pdf_convert import ConvertedDoc, split_into_segments
from app.services.pdf_import import Segment


def test_unmatched_entry_on_its_own_page_is_page_anchored():
    # Docling emitted a heading for "Alpha" but NOT for "Beta". Because "Beta" is a
    # distinct outline entry on its own page (page 1), it still becomes its own
    # article anchored at that page — every TOC item gets its real content, rather
    # than being dropped and absorbed into the previous section. (The outline's
    # bookmark page numbers are authoritative; Docling's heading detection is not.)
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

    assert [s.title for s in segs] == ["Alpha", "Beta"]
    assert "alpha body text" in segs[0].markdown
    assert "beta body text" not in segs[0].markdown        # no bleed into Alpha
    assert "beta body text" in segs[1].markdown            # Beta got its page's content


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


def test_unmatched_entry_does_not_cascade_to_a_distant_heading():
    # THE regression that collapsed the Avamar guide: an early outline entry with
    # no heading on its own page ("Preface", page 0) must NOT match a same-word
    # heading many pages away ("## Preface conventions", page 8). Doing so dragged
    # the monotonic cursor to the end and orphaned every page in between. With
    # page-anchored matching, "Preface" is anchored at its own page 0 (it gets that
    # page's content), and the later, correctly-placed entries each get their own
    # segment — no cascade, no orphaned pages.
    lines = [
        "Preface intro text", "",                     # page 0  (lines 0-1)
        "## Overview", "overview body", "",           # page 1  (2-4)
        "filler p2", "",                              # page 2  (5-6)
        "filler p3", "",                              # page 3  (7-8)
        "filler p4", "",                              # page 4  (9-10)
        "filler p5", "",                              # page 5  (11-12)
        "filler p6", "",                              # page 6  (13-14)
        "filler p7", "",                              # page 7  (15-16)
        "## Preface conventions", "preface conv body", "",  # page 8  (17-19)
    ]
    converted = ConvertedDoc(
        markdown="\n".join(lines), headings=[],
        page_texts=["\n".join(lines)], table_pages=set(),
        page_line_starts=[0, 2, 5, 7, 9, 11, 13, 15, 17],
    )
    outline = [
        Segment(title="Preface", level=1, page_start=0, page_end=0, path=["Preface"]),
        Segment(title="Overview", level=2, page_start=1, page_end=1, path=["Overview"]),
        Segment(title="Preface conventions", level=2, page_start=8, page_end=8,
                path=["Preface conventions"]),
    ]
    segs = split_into_segments(converted, outline)
    assert [s.title for s in segs] == ["Preface", "Overview", "Preface conventions"]
    # "Preface" anchored to page 0 — NOT dragged to the page-8 heading.
    assert "Preface intro text" in segs[0].markdown
    assert "preface conv body" not in segs[0].markdown
    # No orphaned pages: 1-7 land under Overview, page 8 under its own entry.
    assert "overview body" in segs[1].markdown and "filler p7" in segs[1].markdown
    assert "filler p7" not in segs[2].markdown
    assert "preface conv body" in segs[2].markdown


def test_title_does_not_match_longer_subheading_via_containment():
    # THE 'Avamar server' bug: the outline entry "Avamar server" has no heading of
    # its own on its page, but Docling emitted a longer SUB-heading, "## Avamar
    # server functional blocks". Matching title ⊆ heading pointed the article at
    # that sub-heading (a 371-byte fragment) and stranded the real section. The
    # article must instead anchor at the section's page top and contain the whole
    # section — the sub-heading included, not used as the boundary.
    lines = [
        "cover", "",                                              # page 0 (0-1)
        "Introductory paragraph about the server component.",     # page 1 (2)
        "",                                                       # (3)
        "## Avamar server functional blocks",                     # (4)
        "The major functional blocks include the data server.",   # (5)
        "",
    ]
    converted = ConvertedDoc(
        markdown="\n".join(lines), headings=[],
        page_texts=["\n".join(lines)], table_pages=set(), page_line_starts=[0, 2],
    )
    outline = [
        Segment(title="Avamar server", level=3, page_start=1, page_end=1,
                path=["Avamar server"]),
    ]
    segs = split_into_segments(converted, outline)
    assert [s.title for s in segs] == ["Avamar server"]
    assert segs[0].page_start == 1
    # Anchored at page top — captures the intro that precedes the sub-heading …
    assert "Introductory paragraph about the server component." in segs[0].markdown
    # … and still includes the sub-heading's content (grouped, not stranded).
    assert "functional blocks include the data server" in segs[0].markdown


def test_same_page_sibling_without_heading_is_grouped_not_fragmented():
    # "Data server" is a distinct outline entry but shares a page with "Avamar
    # server" and has no heading Docling detected. It cannot be split off without a
    # heading, so its text stays grouped under "Avamar server" rather than being
    # torn out by a 1-line page guess (which would fragment the page into garbage).
    lines = ["## Avamar server", "server intro", "", "data server details here", ""]
    converted = ConvertedDoc(
        markdown="\n".join(lines), headings=[],
        page_texts=["\n".join(lines)], table_pages=set(), page_line_starts=[0],
    )
    outline = [
        Segment(title="Avamar server", level=3, page_start=0, page_end=0,
                path=["Avamar server"]),
        Segment(title="Data server", level=5, page_start=0, page_end=0,
                path=["Avamar server", "Data server"]),
    ]
    segs = split_into_segments(converted, outline)
    assert [s.title for s in segs] == ["Avamar server"]
    assert "data server details here" in segs[0].markdown
