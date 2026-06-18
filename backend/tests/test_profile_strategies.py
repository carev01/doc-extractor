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


# Hub fixture with title+description concatenation (mirrors Intercom layout).
HUB_WITH_DESC = {
    "https://x/": (
        '<a class="cat" href="https://x/c1">'
        '  <span data-testid="cat-name">Clean Cat1</span>'
        '  <p>This is the description for Cat1</p>'
        '</a>'
        '<a class="cat" href="https://x/c2">'
        '  <span data-testid="cat-name">Clean Cat2</span>'
        '  <p>Description for Cat2</p>'
        '</a>'
    ),
    "https://x/c1": (
        '<a class="art" href="https://x/c1/a">'
        '  <span class="title">Art A</span>'
        '  <span data-testid="art-desc">Description A</span>'
        '</a>'
        '<a class="art" href="https://x/c1/b">'
        '  <span class="title">Art B</span>'
        '  <span data-testid="art-desc">Description B</span>'
        '</a>'
    ),
    "https://x/c2": (
        '<a class="art" href="https://x/c2/a">'
        '  <span class="title">Art C</span>'
        '  <span data-testid="art-desc">Description C</span>'
        '</a>'
    ),
}


@pytest.mark.asyncio
async def test_hubspoke_category_title_selector_extracts_clean_title():
    """Regression: category_title_selector must extract the title sub-element
    and avoid concatenating description text."""
    toc = await hubspoke_toc(
        FakeScraper(HUB_WITH_DESC), "https://x/",
        category_link_selector="a.cat",
        article_link_selector="a.art",
        category_title_selector='[data-testid="cat-name"]',
    )
    cat_titles = [e.title for e in toc if e.level == 0]
    assert cat_titles == ["Clean Cat1", "Clean Cat2"]
    # Ensure description text did NOT bleed into title
    for title in cat_titles:
        assert "description" not in title.lower()


@pytest.mark.asyncio
async def test_hubspoke_article_title_selector_extracts_clean_title():
    """Regression: article_title_selector must extract the title sub-element
    and avoid concatenating description text."""
    toc = await hubspoke_toc(
        FakeScraper(HUB_WITH_DESC), "https://x/",
        category_link_selector="a.cat",
        article_link_selector="a.art",
        article_title_selector="span:not([data-testid])",
    )
    art_titles = [e.title for e in toc if e.is_article]
    assert art_titles == ["Art A", "Art B", "Art C"]
    for title in art_titles:
        assert "description" not in title.lower()


@pytest.mark.asyncio
async def test_hubspoke_title_selector_fallback_when_subelement_missing():
    """Regression: when the title sub-element is absent, fall back to full
    link text (preserving original behaviour for non-matching profiles)."""
    toc = await hubspoke_toc(
        FakeScraper(HUB), "https://x/",
        category_link_selector="a.cat",
        article_link_selector="a.art",
        # Selector that won't match anything in HUB's plain-text anchors
        category_title_selector='[data-testid="no-such-element"]',
    )
    cat_titles = [e.title for e in toc if e.level == 0]
    # Fallback: full anchor text
    assert cat_titles == ["Cat1", "Cat2"]


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


WRAPPED_CHILD = """<html><body><ul id="nav">
<li><a href="/top">Top</a></li>
<li><a href="/section">Section</a><nav><ul>
  <li><a href="/section/child">Child</a></li>
</ul></nav></li>
</ul></body></html>"""


@pytest.mark.asyncio
async def test_sidebar_tree_wrapped_child_ul():
    """Regression: child <ul> wrapped in <nav> (MkDocs Material pattern) must be
    detected via fallback ``li.find('ul')`` and yield nested entries at level+1.
    The parent <li> has both an <a href> (so it is emitted) and a <nav><ul> wrapper
    containing children — mirrors MkDocs Material's section+children pattern."""
    sc = FakeScraper({"https://x/": WRAPPED_CHILD})
    toc = await sidebar_tree_toc(sc, "https://x/", "#nav")
    titles = [e.title for e in toc]
    assert "Top" in titles
    assert "Section" in titles
    assert "Child" in titles
    section = next(e for e in toc if e.title == "Section")
    child = next(e for e in toc if e.title == "Child")
    assert section.level == 0
    assert section.is_article is False  # has children -> section
    assert child.level == 1, f"Expected child at level 1, got {child.level}"
    assert child.is_article is True


@pytest.mark.asyncio
async def test_sitemap_urls_document_order():
    sm = '<urlset><url><loc>https://x/a</loc></url><url><loc>https://x/b</loc></url></urlset>'
    urls = await sitemap_urls(FakeScraper({"https://x/sitemap.xml": sm}), "https://x/docs")
    assert urls == ["https://x/a", "https://x/b"]
