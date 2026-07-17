"""Drift-free page->line alignment (_align_page_lines / _lis_keep).

Rebuilds a fitz-indexed page_line_starts from verified json/fitz anchors so an
outline entry's fitz page indexes the right markdown line even when docling drops
or merges page-break markers. See pdf_convert._align_page_lines."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.services.pdf_convert as pc


def test_lis_keep_drops_only_the_violator():
    # 5,1,2,3,4 → the longest increasing run is 1,2,3,4 (indices 1..4); index 0 dropped.
    assert pc._lis_keep([5, 1, 2, 3, 4]) == [1, 2, 3, 4]
    assert pc._lis_keep([1, 2, 3]) == [0, 1, 2]
    assert pc._lis_keep([]) == []


def _elem(text, page1):
    return {"text": text, "prov": [{"page_no": page1}]}


def test_align_is_fitz_length_and_corrects_drift():
    # 4 fitz pages; docling produced markdown only for pages 0,1,3 (page 2 empty/
    # merged), so the marker-based seg_starts has just 3 entries and drifts.
    lines = [
        "## Alpha Section",                     # 0  page0 heading (short, not an anchor)
        "alpha body content that is unique aaa",  # 1  page0 anchor
        "## Beta Section",                      # 2  page1 heading
        "beta body content that is unique bbb",   # 3  page1 anchor
        "## Delta Section",                     # 4  page3 heading
        "delta body content that is unique ddd",  # 5  page3 anchor
    ]
    json_texts = [
        _elem("alpha body content that is unique aaa", 1),  # page0
        _elem("beta body content that is unique bbb", 2),   # page1
        _elem("delta body content that is unique ddd", 4),  # page3
    ]
    page_texts = [
        "Alpha Section alpha body content that is unique aaa",
        "Beta Section beta body content that is unique bbb",
        "image only page nothing in markdown here",           # page2: not in markdown
        "Delta Section delta body content that is unique ddd",
    ]
    seg_starts = [0, 2, 4]  # drifted: 3 entries for 4 pages

    out = pc._align_page_lines(lines, json_texts, page_texts, 4, seg_starts)

    assert len(out) == 4                       # fitz-length, not the drifted 3
    assert out == sorted(out)                  # monotone non-decreasing
    # Each anchored page's line carries that page's unique content.
    assert "alpha body content" in lines[out[0]]
    assert "beta body content" in lines[out[1]]
    assert "delta body content" in lines[out[3]]
    # The empty page 2 is bracketed between beta(line3) and delta(line5).
    assert out[1] <= out[2] <= out[3]


def test_align_falls_back_when_too_few_anchors():
    # No usable anchors (all json text too short / not in markdown) → return
    # seg_starts unchanged (the marker behavior), so no document regresses.
    lines = ["## H", "x", "## H2", "y"]
    json_texts = [_elem("H", 1), _elem("H2", 2)]           # too short to anchor
    page_texts = ["H x", "H2 y"]
    seg_starts = [0, 2]
    assert pc._align_page_lines(lines, json_texts, page_texts, 2, seg_starts) is seg_starts


def test_align_lis_rejects_a_stray_anchor():
    # A false anchor whose line order contradicts its page order is dropped by LIS,
    # not allowed to shift other pages.
    lines = [
        "aaaa unique content for page zero here",   # 0
        "bbbb unique content for page one here",     # 1
        "cccc unique content for page two here",     # 2
    ]
    json_texts = [
        _elem("aaaa unique content for page zero here", 1),   # page0 -> line0 OK
        _elem("cccc unique content for page two here", 2),    # page1 -> line2 (too late/stray)
        _elem("bbbb unique content for page one here", 3),     # page2 -> line1 (violates order)
    ]
    page_texts = ["aaaa unique content for page zero here",
                  "x", "y"]
    seg_starts = [0, 1, 2]
    out = pc._align_page_lines(lines, json_texts, page_texts, 3, seg_starts)
    assert len(out) == 3
    assert out == sorted(out)   # still monotone despite the stray anchor
