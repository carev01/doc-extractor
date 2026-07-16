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

from app.services.pdf_convert import (
    ConvertedDoc, DocHeading, split_into_segments, split_pages,
    _heading_line_by_page, _fitz_page_line_starts,
)
from app.services.pdf_import import Segment


# ── heading-anchored page mapping (immune to page_line_starts drift) ──

_DRIFT_LINES = [
    "# Chapter A",        # 0  page 0
    "intro alpha",        # 1
    "",                   # 2
    "## Section One",     # 3  page 1
    "content one aaa",    # 4
    "",                   # 5
    "## Section Two",     # 6  page 2
    "content two bbb",    # 7
    "",                   # 8
    "## Section Three",   # 9  page 3
    "content three ccc",  # 10
]
_DRIFT_HEADINGS = [
    DocHeading(text="Chapter A", level=1, page0=0),
    DocHeading(text="Section One", level=2, page0=1),
    DocHeading(text="Section Two", level=2, page0=2),
    DocHeading(text="Section Three", level=2, page0=3),
]
_DRIFT_OUTLINE = [
    Segment(title="Chapter A", level=1, page_start=0, page_end=0, path=["Chapter A"]),
    Segment(title="Section One", level=2, page_start=1, page_end=1, path=["Chapter A", "Section One"]),
    Segment(title="Section Two", level=2, page_start=2, page_end=2, path=["Chapter A", "Section Two"]),
    Segment(title="Section Three", level=2, page_start=3, page_end=3, path=["Chapter A", "Section Three"]),
]
# DRIFTED page_line_starts: docling dropped/merged markers, so every later page
# collapses onto Section Three's line (line 9) — the marker-based anchor is wrong.
_DRIFTED_STARTS = [0, 9, 9, 9]


def test_heading_line_by_page_maps_reliable_json_pages():
    m = _heading_line_by_page(_DRIFT_LINES, _DRIFT_HEADINGS)
    assert m == {0: 0, 1: 3, 2: 6, 3: 9}


def test_heading_line_by_page_skips_unmatched_and_handles_dupes():
    lines = ["## Alpha", "x", "## Beta", "y", "## Alpha", "z"]
    heads = [DocHeading("Alpha", 2, 0), DocHeading("Gamma", 2, 1),  # Gamma not in md
             DocHeading("Beta", 2, 2), DocHeading("Alpha", 2, 4)]   # 2nd Alpha → 2nd occurrence
    assert _heading_line_by_page(lines, heads) == {0: 0, 2: 2, 4: 4}


def test_heading_anchor_beats_drifted_page_line_starts():
    # With correct json headings, each entry anchors to its real page despite the
    # drifted page_line_starts → all four sections become their own articles.
    converted = ConvertedDoc(
        markdown="\n".join(_DRIFT_LINES), headings=_DRIFT_HEADINGS,
        page_texts=["p0", "p1", "p2", "p3"], table_pages=set(),
        page_line_starts=_DRIFTED_STARTS,
    )
    segs = split_into_segments(converted, _DRIFT_OUTLINE)
    assert [s.title for s in segs] == ["Chapter A", "Section One", "Section Two", "Section Three"]
    one = next(s for s in segs if s.title == "Section One")
    assert "content one aaa" in one.markdown and "content two bbb" not in one.markdown


_PAGE_SLICE_LINES = [
    "# Page Zero",     # 0  fitz page 0 (heading)
    "zero body",       # 1
    "one body",        # 2  fitz page 1 — MERGED into page 0's segment (no marker)
    "## Page Two",     # 3  fitz page 2 (heading)
    "two body",        # 4
    "## Page Three",   # 5  fitz page 3 (heading)
    "three body",      # 6
]
_PAGE_SLICE_HEADINGS = [
    DocHeading(text="Page Zero", level=1, page0=0),
    DocHeading(text="Page Two", level=2, page0=2),
    DocHeading(text="Page Three", level=2, page0=3),
]


def test_fitz_page_line_starts_interpolates_full_length():
    # 4 fitz pages, anchors on 0,2,3 → a full-length map with page 1 interpolated.
    out = _fitz_page_line_starts({0: 0, 2: 3, 3: 5}, total_pages=4, n_lines=7)
    assert len(out) == 4 and out == sorted(out) and out[0] == 0
    assert out[1] and 0 < out[1] <= 3           # page 1 placed between anchors 0 and 2


def test_fitz_page_line_starts_needs_two_anchors():
    assert _fitz_page_line_starts({0: 0}, 4, 7) is None   # too few → caller falls back


def test_split_pages_is_fitz_aligned_via_heading_anchors():
    # page_line_starts is DRIFTED (3 markers for 4 pages — docling merged page 1),
    # but heading anchors let split_pages emit one slice PER FITZ PAGE, so the
    # escalation scorer's page index lines up with page_texts/table_pages again.
    conv = ConvertedDoc(
        markdown="\n".join(_PAGE_SLICE_LINES), headings=_PAGE_SLICE_HEADINGS,
        page_texts=["p0", "p1", "p2", "p3"], table_pages=set(),
        page_line_starts=[0, 3, 5],   # drifted: 3 entries for 4 fitz pages
    )
    pages = split_pages(conv)
    assert len(pages) == 4                                  # fitz-aligned, not 3
    assert "one body" in pages[1]                           # merged page 1 is its own slice
    assert "Page Two" in pages[2] and "Page Three" in pages[3]


def test_split_pages_falls_back_to_markers_without_anchors():
    conv = ConvertedDoc(
        markdown="\n".join(_PAGE_SLICE_LINES), headings=[],
        page_texts=["p0", "p1", "p2", "p3"], table_pages=set(),
        page_line_starts=[0, 3, 5],
    )
    assert len(split_pages(conv)) == 3     # no anchors → marker-based (drifted) slices


def test_drift_without_heading_anchors_folds_a_section():
    # Same drift but NO json headings → falls back to the drifted page_line_starts,
    # whose collapsed anchors push a real heading out of the search window, so a
    # section folds instead of opening its own article. Documents the bug the
    # heading anchor fixes (and proves the fix, not the fixture, is what resolves it).
    converted = ConvertedDoc(
        markdown="\n".join(_DRIFT_LINES), headings=[],
        page_texts=["p0", "p1", "p2", "p3"], table_pages=set(),
        page_line_starts=_DRIFTED_STARTS,
    )
    titles = [s.title for s in split_into_segments(converted, _DRIFT_OUTLINE)]
    assert titles != ["Chapter A", "Section One", "Section Two", "Section Three"]


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


def test_articles_are_detected_sections_subentries_fold_into_covering_article():
    # THE real Avamar shape: a section Docling gave a heading ("functional blocks")
    # on one page, then fine sub-entries with NO heading (Data server, MCS) on the
    # next page, then another detected section ("Avamar clients") further down that
    # page. Articles are the DETECTED sections only; the sub-entries are NOT their
    # own articles — their text folds into the article that covers their page (here
    # "functional blocks"), so nothing is lost. Each segment records the outline
    # index that starts it (so pdf_import can build the full-outline TOC and link
    # every entry — including Data server/MCS — to its covering article).
    lines = [
        "# Introduction", "intro body", "",                 # page 0 (0-2)
        "## functional blocks", "fb body", "",              # page 1 (3-5)
        "data server description", "",                       # page 2 (6-7)
        "mcs description", "",                               # (8-9)
        "## Avamar clients", "clients body", "",             # (10-12)
    ]
    converted = ConvertedDoc(
        markdown="\n".join(lines), headings=[],
        page_texts=["\n".join(lines)], table_pages=set(),
        page_line_starts=[0, 3, 6],
    )
    outline = [
        Segment(title="Introduction", level=1, page_start=0, page_end=0, path=["Introduction"]),
        Segment(title="functional blocks", level=4, page_start=1, page_end=1,
                path=["Introduction", "functional blocks"]),
        Segment(title="Data server", level=5, page_start=2, page_end=2,
                path=["Introduction", "functional blocks", "Data server"]),
        Segment(title="Management Console Server (MCS)", level=5, page_start=2, page_end=2,
                path=["Introduction", "functional blocks", "Management Console Server (MCS)"]),
        Segment(title="Avamar clients", level=4, page_start=2, page_end=2, path=["Introduction", "Avamar clients"]),
    ]
    segs = split_into_segments(converted, outline)
    # Only detected sections are articles — Data server / MCS are NOT.
    assert [s.title for s in segs] == ["Introduction", "functional blocks", "Avamar clients"]
    # Each article knows which outline entry starts it (for TOC linking).
    assert [s.outline_index for s in segs] == [0, 1, 4]
    # The sub-entries' text is preserved in the covering ("functional blocks") article.
    assert "data server description" in segs[1].markdown
    assert "mcs description" in segs[1].markdown
    # …and not leaked into the next detected section.
    assert "data server description" not in segs[2].markdown
