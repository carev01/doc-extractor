import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.pdf_convert import ConvertedDoc, RenderedSegment
from app.services.pdf_escalate import score_segment


def _conv(page_texts, table_pages=None):
    return ConvertedDoc(markdown="", headings=[], page_texts=page_texts,
                        table_pages=table_pages or set(), images=[], engine="docling")


def _seg(md, p0=0, p1=0):
    return RenderedSegment(title="t", level=1, path=["t"], page_start=p0,
                           page_end=p1, markdown=md, images=[])


def test_clean_table_is_confident():
    md = "## t\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n"
    assert score_segment(_seg(md), _conv(["x" * 50])) == []


def test_ragged_table_flagged():
    md = "## t\n\n| a | b |\n| --- | --- |\n| 1 | 2 | 3 |\n"
    assert "ragged_table" in score_segment(_seg(md), _conv(["x" * 50]))


def test_missing_table_flagged():
    md = "## t\n\njust prose, no table\n"
    assert "missing_table" in score_segment(_seg(md), _conv(["x" * 50], table_pages={0}))


def test_sparse_text_flagged():
    md = "## t\n\ntiny\n"
    assert "sparse_text" in score_segment(_seg(md), _conv(["y" * 1000]))


def test_empty_pages_flagged_for_near_empty_multipage_segment():
    # 6 pages, ~9 chars of markdown, and NO text layer (page_texts empty) so
    # sparse_text can't fire — the image-only case that silently lost content.
    md = "## Cloud\n"
    issues = score_segment(_seg(md, p0=0, p1=5), _conv([""] * 6))
    assert "empty_pages" in issues


def test_empty_pages_not_flagged_when_populated():
    md = "## t\n\n" + ("real content " * 200)  # well over 30 chars/page
    assert "empty_pages" not in score_segment(_seg(md, p0=0, p1=5), _conv([""] * 6))


def test_empty_pages_not_flagged_for_single_page():
    # A single sparse page is normal (a section title page); only multi-page
    # near-empty runs are suspicious.
    assert "empty_pages" not in score_segment(_seg("x", p0=3, p1=3), _conv([""] * 4))
