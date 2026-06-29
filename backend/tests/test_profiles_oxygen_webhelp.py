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
    assert 401 in p.raw_http_retry_statuses
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
