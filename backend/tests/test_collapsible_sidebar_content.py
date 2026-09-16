"""Cohesity (collapsible_sidebar) content comes from a direct GET, not a render.

Why it had to change: Firecrawl renders through Browserless, whose Chromium this
portal 403s, and Firecrawl's per-request ``headers`` cannot fix that — Playwright
ignores a User-Agent set as an extra HTTP header, it has to be set on the browser
context. So every one of 9,931 pages returned 200-with-empty-content and was
skipped, leaving articles_extracted at 0.

That was worse than a plain failure: with every page skipped,
``process_article_result`` returns "empty" before any DB write, so no surviving
article's ``source_url`` advanced to the new version's URL — and
``_reconcile_removals`` matches survivors *by* ``source_url``. A completed run
would have flagged all 14,808 articles removed.

The body is fully server-rendered, so raw_http is both correct and ~20-50x
faster than a render.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.firecrawl import _select_content_path
from app.services.profiles.collapsible_sidebar import PROFILE

URL = "https://docs.cohesity.com/docs/netbackup/11.2.0.1/103228346-173151032-0/v1-173151032"

# A leaf topic: real prose, as served (no JS needed).
LEAF = """<html><body><div data-slot="sidebar-inner"><a href="/x">nav</a></div>
<main><article class="prose prose-slate">
<h1>Recommendations of NetBackup deployment</h1>
<p>Note the following recommendations:</p>
<ul><li>Do not delete the disk linked to PV.</li><li>Ensure one operator.</li></ul>
<img src="/media/img/topology.png" alt="topology">
</article></main></body></html>"""

# A section page: legitimately just a list of child topics.
SECTION = """<html><body><main><article class="prose">
<h1>Recommendations and Limitations</h1>
<p>This section includes the following topics:</p>
<ul><li>Recommendations</li><li>Limitations</li></ul>
</article></main></body></html>"""

# What the edge serves when it blocks the client.
FORBIDDEN = "<html><head><title>403 Forbidden</title></head><body>403 Forbidden</body></html>"


def test_content_is_routed_through_raw_http():
    assert PROFILE.content_engine == "raw_http"
    assert _select_content_path(False, PROFILE.content_engine, None) == "raw_http"


def test_a_render_is_never_chosen_even_when_authenticated():
    # raw_http wins unconditionally; a future auth realm on this source must not
    # silently route content back through the renderer that gets 403d.
    assert _select_content_path(True, PROFILE.content_engine, None) == "raw_http"
    assert _select_content_path(True, PROFILE.content_engine, "browserless") == "raw_http"


def test_a_leaf_topic_scopes_to_its_article_body():
    out = PROFILE.extract_content_html(LEAF, URL)
    assert out is not None
    assert "Note the following recommendations" in out
    assert "Do not delete the disk" in out
    # The sidebar must not be dragged into every page.
    assert "sidebar-inner" not in out and ">nav<" not in out


def test_relative_images_are_absolutised():
    out = PROFILE.extract_content_html(LEAF, URL)
    assert "https://docs.cohesity.com/media/img/topology.png" in out


def test_an_absolute_image_is_left_alone():
    html = LEAF.replace("/media/img/topology.png", "https://cdn.example.com/a.png")
    out = PROFILE.extract_content_html(html, URL)
    assert "https://cdn.example.com/a.png" in out


def test_a_thin_section_page_is_content_not_a_miss():
    """Section pages carry only a child-topic list. Treating their thinness as a
    scope miss would fail the run on a third of a healthy doc set."""
    out = PROFILE.extract_content_html(SECTION, URL)
    assert out is not None
    assert "This section includes the following topics" in out


def test_a_blocked_page_yields_none_so_the_guard_can_see_it():
    """The old path got 200-with-empty from Firecrawl and skipped each page
    individually, so a site-wide block produced no failure signal at all. None
    makes it a scope miss, which the raw_http failure-rate guard aborts on."""
    assert PROFILE.extract_content_html(FORBIDDEN, URL) is None


def test_a_page_without_an_article_yields_none():
    assert PROFILE.extract_content_html("<html><body><p>hi</p></body></html>", URL) is None


def test_the_toc_still_uses_the_browser():
    # The sidebar tree mounts on click, so TOC discovery must keep rendering —
    # only the content path moved.
    assert hasattr(PROFILE, "build_toc")
    assert PROFILE.content_config()["includeTags"] == ["article"]
