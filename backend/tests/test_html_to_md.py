"""Tests for html_to_markdown — GFM-table normalisation before markdownify.

markdownify omits the GFM header separator (``| --- |``) when the header row has
a previous sibling (e.g. a ``<caption>`` or ``<colgroup>``), so the table fails
to render. We lift captions out and wrap header rows in ``<thead>`` so the
separator is always emitted, while leaving table-free content untouched.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from markdownify import markdownify

from app.services.html_to_md import html_to_markdown


def test_caption_table_renders_with_separator_and_bold_caption():
    # Veeam's shape: <table><caption>…</caption><tr><th>…
    html = (
        '<table><caption>Dell Unity XT</caption>'
        '<tr><th><p>From</p></th><th><p>To</p></th></tr>'
        '<tr><td><p>Backup server</p></td><td><p>Storage</p></td></tr></table>'
    )
    md = html_to_markdown(html)
    assert "| --- | --- |" in md           # GFM separator now present → renders
    assert "**Dell Unity XT**" in md        # caption kept as a bold heading
    assert "| From | To |" in md
    assert "| Backup server | Storage |" in md
    # The bold caption precedes the table.
    assert md.index("**Dell Unity XT**") < md.index("| From | To |")


def test_plain_markdownify_misses_separator_without_fix():
    # Guards the premise: the same caption table via bare markdownify has no
    # separator (so this isn't a no-op fix).
    html = (
        '<table><caption>Cap</caption>'
        '<tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>'
    )
    assert "| --- |" not in markdownify(html)
    assert "| --- |" in html_to_markdown(html)


def test_thead_wrapping_handles_colgroup_before_header():
    html = (
        '<table><colgroup><col/><col/></colgroup>'
        '<tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>'
    )
    assert "| --- | --- |" in html_to_markdown(html)


def test_non_table_content_is_byte_identical_to_markdownify():
    html = "<h1>Title</h1><p>Some <strong>prose</strong> with a <a href='/x'>link</a>.</p><ul><li>a</li><li>b</li></ul>"
    assert html_to_markdown(html) == markdownify(html).strip()


def test_already_thead_table_still_renders():
    html = (
        '<table><thead><tr><th>A</th><th>B</th></tr></thead>'
        '<tbody><tr><td>1</td><td>2</td></tr></tbody></table>'
    )
    assert "| --- | --- |" in html_to_markdown(html)


def test_empty_returns_empty():
    assert html_to_markdown("") == ""
    assert html_to_markdown(None) == ""
