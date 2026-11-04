"""MindTouch (CXone Expert) profile: TOC from the deki page-tree API, content
scoped to .mt-content-container (e.g. docs.connectwise.com/x360Recover)."""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.profiles import registry
from app.services.profiles.mindtouch import (
    MindTouchProfile,
    parse_mindtouch_tree,
    tree_api_url,
)
from app.services.profiles.scraper import FakeScraper

ROOT = "https://docs.connectwise.com/x360Recover"


def _page(title, path, subpages="", deleted="false"):
    return {
        "@deleted": deleted,
        "title": title,
        "path": {"#text": path},
        "uri.ui": f"https://docs.connectwise.com/{path}",
        "subpages": subpages,
    }


# The API's XML-to-JSON quirks: several children are a list, a single child is a
# bare dict, and no children is an empty string.
TREE = {"page": _page("x360Recover", "x360Recover", {"page": [
    _page("Release Notes", "x360Recover/Release_Notes", {"page":
        _page("Release Notes 2026", "x360Recover/Release_Notes/2026")}),
    _page("Retired", "x360Recover/Retired", deleted="true"),
    _page("x360Recover Agent", "x360Recover/x360Recover_Agent", {"page": [
        _page("(Linux) Install an Agent", "x360Recover/x360Recover_Agent/Linux"),
        _page("(Windows) Install an Agent", "x360Recover/x360Recover_Agent/Windows"),
    ]}),
]})}


def test_tree_api_url_targets_the_section_path():
    assert tree_api_url(ROOT) == (
        "https://docs.connectwise.com/@api/deki/pages/=x360Recover/tree?dream.out.format=json")
    # Nested sections are double-encoded, as the deki API requires.
    assert tree_api_url("https://docs.connectwise.com/Axcient/Backup_for_Files/") == (
        "https://docs.connectwise.com/@api/deki/pages/=Axcient%252FBackup_for_Files"
        "/tree?dream.out.format=json")


def test_tree_keeps_vendor_order_hierarchy_and_parent_pages():
    toc = parse_mindtouch_tree(TREE)
    assert [(e.title, e.level, e.parent_url) for e in toc] == [
        ("x360Recover", 0, None),
        ("Release Notes", 0, None),
        ("Release Notes 2026", 1, "https://docs.connectwise.com/x360Recover/Release_Notes"),
        ("x360Recover Agent", 0, None),
        ("(Linux) Install an Agent", 1, "https://docs.connectwise.com/x360Recover/x360Recover_Agent"),
        ("(Windows) Install an Agent", 1, "https://docs.connectwise.com/x360Recover/x360Recover_Agent"),
    ]
    # Guide/category pages carry their own (often substantial) content, so every
    # node is an article — none is a url-less heading.
    assert all(e.is_article and e.url for e in toc)


def test_tree_skips_deleted_pages():
    assert "Retired" not in [e.title for e in parse_mindtouch_tree(TREE)]


def test_build_toc_fetches_the_tree_inside_a_guest_session():
    scraper = FakeScraper({}, raw_by_url={tree_api_url(ROOT): json.dumps(TREE)})
    toc = asyncio.run(MindTouchProfile().build_toc(ROOT, scraper))
    assert len(toc) == 6
    # The API 403s without the guest session a page visit mints first.
    assert scraper.sessions == [(ROOT, tree_api_url(ROOT))]


def test_build_toc_returns_empty_on_an_unusable_tree():
    scraper = FakeScraper({}, raw_by_url={tree_api_url(ROOT): "<html>login</html>"})
    assert asyncio.run(MindTouchProfile().build_toc(ROOT, scraper)) == []


PAGE = """<html><head><meta name="generator" content="MindTouch"></head><body>
<h1>(Linux) Install an Agent</h1>
<section class="mt-content-container">
<div class="mt-section"><h2 class="editable">Overview</h2>
<p>Before you can begin taking backups, install the agent.</p>
<img src="/@api/deki/files/69063/shot.png?revision=1" alt="shot">
<div class="noindex"><ul><li><a href="/x360Recover/a">Child A</a> No image available</li></ul></div>
<script>Deki.init()</script>
</div>
<footer class="mt-content-footer">Was this helpful?</footer>
</section></body></html>"""


def test_content_is_scoped_and_generated_parts_dropped():
    url = "https://docs.connectwise.com/x360Recover/x360Recover_Agent/Linux"
    html = MindTouchProfile().extract_content_html(PAGE, url)
    assert "install the agent" in html
    assert 'src="https://docs.connectwise.com/@api/deki/files/69063/shot.png?revision=1"' in html
    for gone in ("No image available", "Deki.init", "Was this helpful", "<h1>"):
        assert gone not in html


def test_page_without_content_container_scopes_to_none():
    assert MindTouchProfile().extract_content_html("<html><body>login</body></html>", ROOT) is None


def test_detects_mindtouch_and_is_registered():
    p = MindTouchProfile()
    assert p.detect(PAGE, ROOT)
    assert not p.detect("<html><body>docs</body></html>", ROOT)
    assert p.content_engine == "raw_http"
    assert registry.get("mindtouch") is not None
