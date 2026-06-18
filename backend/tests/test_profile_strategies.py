import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.services.profiles.scraper import FakeScraper
from app.services.profiles.strategies import sidebar_tree_toc, hubspoke_toc, sitemap_urls

NEST = """<html><body><nav id="t">
<ul><li><a href="/a">A</a><ul>
  <li><a href="/a/1">A1</a></li><li><a href="/a/2">A2</a></li></ul></li>
<li><a href="/b">B</a></li></ul></nav></body></html>"""


@pytest.mark.asyncio
async def test_sidebar_tree_levels_and_order():
    sc = FakeScraper({"https://x/": NEST})
    toc = await sidebar_tree_toc(sc, "https://x/", "#t")
    assert [(e.title, e.level) for e in toc] == [("A", 0), ("A1", 1), ("A2", 1), ("B", 0)]
    assert toc[1].parent_url.endswith("/a")
    assert toc[0].is_article is False   # A has children -> section
    assert toc[3].is_article is True    # B is a leaf -> article


HUB = {
    "https://x/": '<a class="cat" href="https://x/c1">Cat1</a><a class="cat" href="https://x/c2">Cat2</a>',
    "https://x/c1": '<a class="art" href="https://x/c1/a">C1A</a><a class="art" href="https://x/c1/b">C1B</a>',
    "https://x/c2": '<a class="art" href="https://x/c2/a">C2A</a>',
}


@pytest.mark.asyncio
async def test_hubspoke_order_and_hierarchy():
    toc = await hubspoke_toc(
        FakeScraper(HUB), "https://x/",
        category_link_selector="a.cat", article_link_selector="a.art",
    )
    assert [(e.title, e.level, e.is_article) for e in toc] == [
        ("Cat1", 0, False), ("C1A", 1, True), ("C1B", 1, True),
        ("Cat2", 0, False), ("C2A", 1, True),
    ]


UL_AS_ROOT = """<html><body>
<ul id="m"><li><a href="/x">X</a></li><li><a href="/y">Y</a></li></ul>
</body></html>"""


@pytest.mark.asyncio
async def test_sidebar_tree_ul_selector_directly():
    """Regression: sidebar_tree_toc must work when the selector matches the <ul> itself."""
    sc = FakeScraper({"https://x/": UL_AS_ROOT})
    toc = await sidebar_tree_toc(sc, "https://x/", "#m")
    assert [e.title for e in toc] == ["X", "Y"]
    assert all(e.is_article for e in toc)


@pytest.mark.asyncio
async def test_sitemap_urls_document_order():
    sm = '<urlset><url><loc>https://x/a</loc></url><url><loc>https://x/b</loc></url></urlset>'
    urls = await sitemap_urls(FakeScraper({"https://x/sitemap.xml": sm}), "https://x/docs")
    assert urls == ["https://x/a", "https://x/b"]
