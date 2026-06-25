"""Tests for the category-accordion help-center extraction profile.

The profile targets help centers that publish one landing page per product,
grouping articles into ``.category-section`` blocks whose ``<label>`` is the
section title and whose ``<p><a>`` children are the articles. Section labels
carry no URL of their own, so they become structural (url-less,
is_article=False) TOC headers; articles are their level-1 children.

The TOC must be scoped to the source's own ``-category`` path so cross-product
nav links and external links never leak in — the original defect that produced
a TOC full of unrelated pages.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.profiles.category_accordion import CategoryAccordionProfile
from app.services.profiles.scraper import FakeScraper

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "platforms")

ROOT = "https://www.keepit.com/help/microsoft-365-category/"


def _read(name: str) -> str:
    return open(os.path.join(FIXTURE_DIR, name), encoding="utf-8").read()


def _scraper() -> FakeScraper:
    return FakeScraper({ROOT: _read("category_accordion.html")})


# ---------------------------------------------------------------------------
# detect()
# ---------------------------------------------------------------------------

def test_detect_true_on_fixture():
    assert CategoryAccordionProfile().detect(_read("category_accordion.html"), ROOT) is True


def test_detect_false_on_foreign_host():
    # Same markup served from another host is not this publisher's help center.
    assert CategoryAccordionProfile().detect(_read("category_accordion.html"), "https://example.com/help/") is False


def test_detect_false_without_category_markup():
    assert CategoryAccordionProfile().detect("<html><body>hello</body></html>", ROOT) is False


def test_detect_false_on_freshdesk():
    assert CategoryAccordionProfile().detect(_read("freshdesk.html"), "https://help.keepit.com/support/home") is False


# ---------------------------------------------------------------------------
# build_toc()
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sections_are_urlless_headers_at_level_0():
    toc = await CategoryAccordionProfile().build_toc(ROOT, _scraper())
    sections = [e for e in toc if e.level == 0]
    assert [e.title for e in sections] == ["Get started", "Recover Your Data"]
    assert all(e.is_article is False for e in sections)
    assert all(not e.url for e in sections)


@pytest.mark.asyncio
async def test_articles_at_level_1_with_absolute_urls():
    toc = await CategoryAccordionProfile().build_toc(ROOT, _scraper())
    articles = [e for e in toc if e.level == 1]
    assert all(e.is_article for e in articles)
    assert all(e.url.startswith("https://www.keepit.com/help/microsoft-365-category/") for e in articles)
    titles = [e.title for e in articles]
    assert "Requirements for setting up a Microsoft 365 backup" in titles
    assert "Restore Microsoft 365 items" in titles


@pytest.mark.asyncio
async def test_cross_product_and_external_links_excluded():
    toc = await CategoryAccordionProfile().build_toc(ROOT, _scraper())
    urls = " ".join(e.url or "" for e in toc)
    assert "salesforce-category" not in urls
    assert "zendesk-category" not in urls
    assert "learn.microsoft.com" not in urls
    assert "platform-category" not in urls


@pytest.mark.asyncio
async def test_duplicate_articles_deduped():
    toc = await CategoryAccordionProfile().build_toc(ROOT, _scraper())
    connector = [e for e in toc if e.url and e.url.endswith("/create-a-microsoft-365-connector/")]
    assert len(connector) == 1


@pytest.mark.asyncio
async def test_dom_order_preserved():
    toc = await CategoryAccordionProfile().build_toc(ROOT, _scraper())
    titles = [e.title for e in toc]
    assert titles == [
        "Get started",
        "Requirements for setting up a Microsoft 365 backup",
        "Create a Microsoft 365 connector",
        "Recover Your Data",
        "Restore Microsoft 365 items",
        "Restore Exchange data from the connector",
    ]


# ---------------------------------------------------------------------------
# content_config()
# ---------------------------------------------------------------------------

def test_content_config_captures_article_body_and_embeds():
    # Prose lives in <article class="m article">; tables/other embedded blocks
    # render in sibling <div class="m embed"> *outside* the article, so both must
    # be included or tables get dropped.
    cfg = CategoryAccordionProfile().content_config()
    assert "article.article" in cfg["includeTags"]
    assert ".m.embed" in cfg["includeTags"]


def test_content_config_excludes_page_chrome():
    # Breadcrumb, the under-title byline, the bottom category chips, the author
    # box, the nav sidebar and the related-articles block are all noise.
    cfg = CategoryAccordionProfile().content_config()
    for sel in (".m.breadcrumb", ".sub", ".tags", ".author",
                ".category-sidebar", ".m.related"):
        assert sel in cfg["excludeTags"], f"{sel} should be excluded"
