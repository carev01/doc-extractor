"""score_page: per-page confidence issues over one page's markdown."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.pdf_convert import ConvertedDoc
from app.services.pdf_escalate import score_page


def _conv(page_texts, table_pages=None):
    return ConvertedDoc(markdown="", headings=[], page_texts=page_texts,
                        table_pages=table_pages or set(), images=[], engine="docling")


def test_clean_table_is_confident():
    md = "## t\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n"
    assert score_page(md, 0, _conv(["x" * 50])) == []


def test_ragged_table_flagged():
    md = "## t\n\n| a | b |\n| --- | --- |\n| 1 | 2 | 3 |\n"
    assert "ragged_table" in score_page(md, 0, _conv(["x" * 50]))


def test_missing_table_flagged():
    md = "## t\n\njust prose, no table\n"
    assert "missing_table" in score_page(md, 0, _conv(["x" * 50], table_pages={0}))


def test_sparse_text_flagged():
    # A real text layer (>200 chars) but the markdown captured < half of it.
    md = "## t\n\ntiny\n"
    assert "sparse_text" in score_page(md, 0, _conv(["y" * 1000]))


def test_empty_image_page_flagged():
    # No usable text layer AND ~no markdown → an image-only page whose content was
    # lost (sparse_text can't catch this — nothing to compare against).
    assert "empty_pages" in score_page("", 0, _conv([""]))


def test_short_text_page_not_flagged():
    # A legitimately short page: little text layer, but the markdown captured it —
    # not content loss, so nothing to escalate.
    md = "## Overview\n\nSee the next section for full details.\n"
    raw = "See the next section for full details."
    assert score_page(md, 0, _conv([raw])) == []


def test_scoring_uses_the_given_page_index():
    # table_pages membership is checked against page_idx, not page 0.
    md = "## t\n\nprose only, no pipe\n"
    conv = _conv(["x" * 50, "x" * 50, "x" * 50], table_pages={2})
    assert "missing_table" not in score_page(md, 0, conv)
    assert "missing_table" in score_page(md, 2, conv)
