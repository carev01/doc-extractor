import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pytest
from app.services.profiles.scraper import FakeScraper
from app.services.profiles.detector import detect_platform
from app.services.profiles.content_scope import scope_content_html
from app.services.profiles.oxygen_webhelp import OxygenWebhelpProfile

ROOT = "https://docs.example.com/en-us/saas/saas/common/getting_started.html"
INVENTORY_URL = "https://docs.example.com/en-us/saas/oxygen-webhelp/app/search/index/htmlFileInfoList.js"

PAGE = """
<html><body>
  <script src="../../oxygen-webhelp/app/commons.js"></script>
  <nav id="wh_publication_toc"><ul><li role="treeitem" data-tocid="t1"><a href="../../saas/common/getting_started.html">Getting Started</a></li></ul></nav>
  <article><h1>Getting Started</h1><p>Body text here.</p></article>
  <div class="wh_breadcrumb">Home</div>
  <footer>footer</footer>
</body></html>
"""
INVENTORY = 'var htmlFileInfoList = ["common/intro.html@@@Intro@@@d", "OLVM/add.html@@@Add OLVM@@@d", "OLVM/edit.html@@@Edit OLVM@@@d"];'

def _scraper():
    return FakeScraper({}, raw_by_url={ROOT: PAGE, INVENTORY_URL: INVENTORY})

def test_opts_into_raw_http_and_attrs():
    p = OxygenWebhelpProfile()
    assert p.content_engine == "raw_http"
    assert p.raw_http_concurrency == 2
    assert p.raw_http_request_delay == 0.3
    assert 401 not in p.raw_http_retry_statuses and 429 in p.raw_http_retry_statuses
    assert p.toc_fragment_selector == "#wh_publication_toc"

def test_detect_needs_both_hooks():
    p = OxygenWebhelpProfile()
    assert p.detect(PAGE, ROOT) is True
    assert p.detect('<div class="oxygen-webhelp"></div>', ROOT) is False
    assert p.detect('<nav id="wh_publication_toc"></nav>', ROOT) is False

def test_detects_via_registry():
    assert detect_platform(PAGE, ROOT) == "oxygen_webhelp"

@pytest.mark.asyncio
async def test_build_toc_from_inventory():
    toc = await OxygenWebhelpProfile().build_toc(ROOT, _scraper())
    # build_toc levels are placeholders (= path.count("/")); the authored
    # hierarchy is set later by the rebuild. What matters here: complete entries
    # with correct titles + absolute URLs resolved against pub_root.
    shape = [(e.level, e.title, e.url) for e in toc]
    assert shape == [
        (1, "Intro", "https://docs.example.com/en-us/saas/common/intro.html"),
        (1, "Add OLVM", "https://docs.example.com/en-us/saas/OLVM/add.html"),
        (1, "Edit OLVM", "https://docs.example.com/en-us/saas/OLVM/edit.html"),
    ]
    assert all(e.is_article for e in toc)

@pytest.mark.asyncio
async def test_build_toc_empty_when_no_oxygen_ref():
    s = FakeScraper({}, raw_by_url={ROOT: "<html><body><article>x</article></body></html>"})
    assert await OxygenWebhelpProfile().build_toc(ROOT, s) == []

def test_content_config_scopes_article():
    cfg = OxygenWebhelpProfile().content_config()
    out = scope_content_html(PAGE, ROOT, cfg["includeTags"], cfg["excludeTags"])
    assert "Body text here." in out
    assert "Getting Started" in out
    assert "Home" not in out         # breadcrumb dropped
    assert "footer" not in out

@pytest.mark.asyncio
async def test_build_toc_empty_when_inventory_unparseable():
    bad = FakeScraper({}, raw_by_url={ROOT: PAGE, INVENTORY_URL: "not a valid htmlFileInfoList file"})
    assert await OxygenWebhelpProfile().build_toc(ROOT, bad) == []

FRAG_SECTION = """
<nav id="wh_publication_toc"><ul>
  <li role="treeitem" data-tocid="root"><div class="topicref"><a href="../root.html">Root</a></div>
    <ul>
      <li role="treeitem" data-tocid="secA"><div class="topicref"><a href="../a/secA.html">Section A</a></div>
        <ul><li role="treeitem" data-tocid="leaf1"><div class="topicref"><a href="../a/leaf1.html">Leaf 1</a></div></li></ul>
      </li>
      <li role="treeitem" data-tocid="secB"><div class="topicref"><a href="../b/secB.html">Section B</a></div></li>
    </ul>
  </li>
</ul></nav>
"""
FRAG_LEAF2 = """
<nav id="wh_publication_toc"><ul>
  <li role="treeitem" data-tocid="root"><div class="topicref"><a href="../root.html">Root</a></div>
    <ul>
      <li role="treeitem" data-tocid="secA"><div class="topicref"><a href="../a/secA.html">Section A</a></div></li>
      <li role="treeitem" data-tocid="secB"><div class="topicref"><a href="../b/secB.html">Section B</a></div>
        <ul><li role="treeitem" data-tocid="leaf2"><div class="topicref"><a href="../b/leaf2.html">Leaf 2</a></div></li></ul>
      </li>
    </ul>
  </li>
</ul></nav>
"""

def test_rebuild_toc_stitches_full_tree():
    base = "https://d.example.com/en-us/saas/saas/"
    frags = [(base + "a/leaf1.html", FRAG_SECTION), (base + "b/leaf2.html", FRAG_LEAF2)]
    toc = OxygenWebhelpProfile().rebuild_toc(frags, base + "common/start.html")
    shape = [(e.level, e.title) for e in toc]
    assert shape == [
        (0, "Root"),
        (1, "Section A"),
        (2, "Leaf 1"),
        (1, "Section B"),
        (2, "Leaf 2"),
    ]
    # parent_url linkage
    by_title = {e.title: e for e in toc}
    assert by_title["Leaf 1"].parent_url == by_title["Section A"].url
    assert by_title["Section B"].parent_url == by_title["Root"].url

def test_rebuild_toc_empty_when_no_fragments():
    assert OxygenWebhelpProfile().rebuild_toc([], "https://d/x.html") == []


# ── Node identity across publishing builds ───────────────────────────────────
# Rubrik's RSC portal is built from pages published in different builds, and
# each build mints its own data-tocid for a topic. Keyed by tocid, the rebuilt
# TOC held every top-level section three times (9,268 entries, 4,891 pages).

BASE = "https://d.example.com/en-us/saas/saas/"


def _li(tocid, href, title, kids=""):
    ul = f"<ul>{kids}</ul>" if kids else ""
    return (f'<li role="treeitem" data-tocid="{tocid}"><div class="topicref">'
            f'<a href="{href}">{title}</a></div>{ul}</li>')


def _nav(*items):
    return f'<nav id="wh_publication_toc"><ul>{"".join(items)}</ul></nav>'


def test_the_same_topic_under_different_tocids_is_one_node():
    """The regression: two builds, two tocid schemes, one tree — not two."""
    build1 = _nav(_li("tocId-d100e1", "root.html", "Root",
                      _li("tocId-d100e2", "a.html", "A")))
    build2 = _nav(_li("tocId-d200e1", "root.html", "Root",
                      _li("root-d200e9", "a.html", "A")))
    toc = OxygenWebhelpProfile().rebuild_toc(
        [(BASE + "root.html", build1), (BASE + "a.html", build2)], BASE)
    assert [(e.level, e.title) for e in toc] == [(0, "Root"), (1, "A")]


def test_a_heading_that_borrows_its_childs_page_keeps_its_level():
    """Oxygen headings often link to their first child's page (Security and
    Users and Access both point at users_access.html). Keyed by URL alone the
    heading swallowed the child and a level of the manual vanished."""
    frag = _nav(_li("s", "users_access.html", "Security",
                    _li("u", "users_access.html", "Users and Access",
                        _li("t", "troubleshooting.html", "Troubleshooting permissions"))))
    toc = OxygenWebhelpProfile().rebuild_toc([(BASE + "troubleshooting.html", frag)], BASE)
    assert [(e.level, e.title) for e in toc] == [
        (0, "Security"), (1, "Users and Access"), (2, "Troubleshooting permissions"),
    ]
    by_title = {e.title: e for e in toc}
    # The page belongs to the real topic; the heading becomes a URL-less section,
    # so the URL still maps to exactly one entry (and one article).
    assert by_title["Security"].url is None and by_title["Security"].is_article is False
    assert by_title["Users and Access"].url == BASE + "users_access.html"
    assert [e.url for e in toc].count(BASE + "users_access.html") == 1


def test_a_collapsed_heading_in_another_fragment_is_still_the_heading():
    """Most fragments show the heading collapsed — no child visible. It must
    still resolve to the same node as the expanded one, not a third copy."""
    expanded = _nav(_li("s1", "users_access.html", "Security",
                        _li("u1", "users_access.html", "Users and Access")))
    collapsed = _nav(_li("s2", "users_access.html", "Security"),
                     _li("w2", "workloads.html", "Workloads"))
    toc = OxygenWebhelpProfile().rebuild_toc(
        [(BASE + "users_access.html", expanded), (BASE + "workloads.html", collapsed)], BASE)
    assert [(e.level, e.title) for e in toc] == [
        (0, "Security"), (1, "Users and Access"), (0, "Workloads"),
    ]


def test_children_seen_in_different_builds_are_merged_not_dropped():
    """'Longest child list wins' orphaned whatever only another build listed,
    and the walk then dumped those at the top level."""
    b1 = _nav(_li("r1", "w.html", "Workloads",
                  _li("a1", "a.html", "A") + _li("b1", "b.html", "B")))
    b2 = _nav(_li("r2", "w.html", "Workloads",
                  _li("a2", "a.html", "A") + _li("c2", "c.html", "C")))
    toc = OxygenWebhelpProfile().rebuild_toc(
        [(BASE + "a.html", b1), (BASE + "c.html", b2)], BASE)
    assert [(e.level, e.title) for e in toc] == [
        (0, "Workloads"), (1, "A"), (1, "B"), (1, "C"),
    ]


def test_the_tree_does_not_depend_on_fragment_order():
    """The caller reads fragments with no ORDER BY."""
    b1 = _nav(_li("r1", "w.html", "Workloads", _li("a1", "a.html", "A") + _li("b1", "b.html", "B")))
    b2 = _nav(_li("r2", "w.html", "Workloads", _li("a2", "a.html", "A") + _li("c2", "c.html", "C")))
    frags = [(BASE + "a.html", b1), (BASE + "c.html", b2)]
    sig = lambda t: [(e.level, e.title, e.url, e.parent_url) for e in t]
    prof = OxygenWebhelpProfile()
    assert sig(prof.rebuild_toc(frags, BASE)) == sig(prof.rebuild_toc(frags[::-1], BASE))


def test_an_item_without_its_own_link_is_not_titled_after_its_child():
    """li.find('a') would pick up the child's anchor for a link-less item."""
    frag = _nav('<li role="treeitem" data-tocid="h"><span>Heading only</span><ul>'
                + _li("c", "child.html", "Child") + "</ul></li>")
    toc = OxygenWebhelpProfile().rebuild_toc([(BASE + "child.html", frag)], BASE)
    assert [e.title for e in toc] == ["Child"]
