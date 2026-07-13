import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.pdf_convert import (
    ConvertedDoc, DocHeading, RenderedImage, split_into_segments,
    _heading_lines, _find_heading_line,
)
from app.services.pdf_import import Segment


def _doc(md, headings=None, table_pages=None):
    return ConvertedDoc(
        markdown=md, headings=headings or [], page_texts=[md],
        table_pages=table_pages or set(), images=[], engine="docling",
    )


def test_outline_split_has_no_cross_section_bleed():
    md = "## Alpha Section\n\nAlpha body.\n\n## Beta Section\n\nBeta body.\n"
    outline = [
        Segment(title="Alpha Section", level=1, page_start=0, page_end=0, path=["Alpha Section"]),
        Segment(title="Beta Section", level=1, page_start=0, page_end=0, path=["Beta Section"]),
    ]
    segs = split_into_segments(_doc(md), outline)
    assert [s.title for s in segs] == ["Alpha Section", "Beta Section"]
    assert "Alpha body." in segs[0].markdown
    assert "Beta" not in segs[0].markdown
    assert "Beta body." in segs[1].markdown
    assert "Alpha body." not in segs[1].markdown


def test_split_never_cuts_a_table():
    md = ("## One\n\nintro\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n| 3 | 4 |\n\n"
          "## Two\n\ntail\n")
    outline = [
        Segment(title="One", level=1, page_start=0, page_end=0, path=["One"]),
        Segment(title="Two", level=1, page_start=0, page_end=0, path=["Two"]),
    ]
    segs = split_into_segments(_doc(md), outline)
    one = next(s for s in segs if s.title == "One").markdown
    assert "| 1 | 2 |" in one and "| 3 | 4 |" in one


def test_no_outline_uses_docling_headings():
    md = "# Title\n\nbody one\n\n# Next\n\nbody two\n"
    headings = [DocHeading("Title", 1, 0), DocHeading("Next", 1, 0)]
    segs = split_into_segments(_doc(md, headings=headings), [])
    assert [s.title for s in segs] == ["Title", "Next"]
    assert "body one" in segs[0].markdown and "body two" in segs[1].markdown


def test_image_assigned_to_owning_segment():
    md = "## A\n\n![x](aa.png)\n\n## B\n\nplain\n"
    outline = [
        Segment(title="A", level=1, page_start=0, page_end=0, path=["A"]),
        Segment(title="B", level=1, page_start=0, page_end=0, path=["B"]),
    ]
    doc = _doc(md)
    doc.images = [RenderedImage(filename="aa.png", data=b"x", alt="x")]
    segs = split_into_segments(doc, outline)
    a = next(s for s in segs if s.title == "A")
    b = next(s for s in segs if s.title == "B")
    assert [i.filename for i in a.images] == ["aa.png"]
    assert b.images == []


def test_numbered_title_matches_spaced_heading():
    lines = "# 1 Preface\n\nbody\n".split("\n")
    hl = _heading_lines(lines)
    # outline title has the number glued + a curly apostrophe variant elsewhere
    assert _find_heading_line(hl, "1Preface", 0) == 0


def test_unmatched_outline_entry_grouped_not_page_fragmented():
    # 'Hidden' has no matching heading in the converted markdown. It must NOT be
    # turned into a page-anchored fragment (that produced garbage articles for
    # finer-grained outlines like Dell manuals) — it's skipped, and its section's
    # text stays under the preceding matched heading. Nothing is lost.
    md = "# Intro\n\nintro body\n\nhidden section body\n"
    conv = ConvertedDoc(markdown=md, headings=[], page_texts=[md, "p2"],
                        table_pages=set(), images=[], engine="docling",
                        page_line_starts=[0, 3])
    outline = [
        Segment(title="Intro", level=1, page_start=0, page_end=0, path=["Intro"]),
        Segment(title="Hidden", level=1, page_start=1, page_end=1, path=["Hidden"]),
    ]
    segs = split_into_segments(conv, outline)
    assert [s.title for s in segs] == ["Intro"]        # no page-anchored "Hidden"
    assert "hidden section body" in segs[0].markdown   # its text grouped under Intro
