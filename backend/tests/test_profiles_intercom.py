"""Tests for the Intercom help-center extraction profile."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.profiles.intercom import IntercomProfile
from app.services.profiles.scraper import FakeScraper

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "platforms")

ROOT = "https://help.druva.com/en/"
COLLECTION_URL = "https://help.druva.com/en/collections/6094377-druva-cloud-platform"


def _read(name: str) -> str:
    return open(os.path.join(FIXTURE_DIR, name), encoding="utf-8").read()


# ---------------------------------------------------------------------------
# detect() — True on intercom fixture, False on all others
# ---------------------------------------------------------------------------

def test_detect_true_on_intercom_fixture():
    assert IntercomProfile().detect(_read("intercom.html"), ROOT) is True


def test_detect_false_on_lazy_tree():
    assert IntercomProfile().detect(_read("lazy_tree.html"), "https://documentation.commvault.com/") is False


def test_detect_false_on_docusaurus():
    assert IntercomProfile().detect(_read("docusaurus.html"), "https://docs.portworx.com/") is False


def test_detect_false_on_mkdocs():
    assert IntercomProfile().detect(_read("mkdocs.html"), "https://docs.example.com/") is False


def test_detect_false_on_gitbook():
    assert IntercomProfile().detect(_read("gitbook.html"), "https://docs.example.com/") is False


def test_detect_false_on_flare_html5():
    assert IntercomProfile().detect(_read("flare_html5.html"), "https://docs.example.com/") is False


def test_detect_false_on_flare_webhelp():
    assert IntercomProfile().detect(_read("flare_webhelp.html"), "https://docs.example.com/") is False


# ---------------------------------------------------------------------------
# content_config()
# ---------------------------------------------------------------------------

def test_content_config():
    cfg = IntercomProfile().content_config()
    assert cfg["onlyMainContent"] is True
    assert cfg["waitFor"] == 1500


# ---------------------------------------------------------------------------
# build_toc() — offline via FakeScraper
#
# The home fixture contains 20 collections; we serve only one real collection
# page (intercom_collection.html = "Druva Cloud Platform") and an empty stub
# for all other collection URLs so hubspoke_toc can iterate without error.
# ---------------------------------------------------------------------------

def _make_scraper() -> FakeScraper:
    home_html = _read("intercom.html")
    collection_html = _read("intercom_collection.html")

    # Build a catch-all stub for collections not in our map
    stub = "<html><body></body></html>"

    # Map the known collection URL to the real fixture; everything else → stub.
    # We import BeautifulSoup only to enumerate all collection hrefs so we can
    # pre-populate the scraper map (FakeScraper returns "" for unknown URLs,
    # which would produce zero articles — fine for the non-target collections).
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(home_html, "html.parser")
    html_by_url: dict[str, str] = {ROOT: home_html}
    for a in soup.select("a.collection-link"):
        href = a.get("href", "")
        if href == COLLECTION_URL:
            html_by_url[href] = collection_html
        else:
            html_by_url[href] = stub

    return FakeScraper(html_by_url)


@pytest.mark.asyncio
async def test_build_toc_collections_at_level_0():
    toc = await IntercomProfile().build_toc(ROOT, _make_scraper())
    collections = [e for e in toc if e.level == 0]
    assert len(collections) == 20
    assert all(not e.is_article for e in collections)


@pytest.mark.asyncio
async def test_build_toc_clean_collection_titles():
    """Collection titles must NOT contain the description or article count."""
    toc = await IntercomProfile().build_toc(ROOT, _make_scraper())
    collections = [e for e in toc if e.level == 0]
    titles = [e.title for e in collections]
    # Verify a known title is present and clean
    assert "Learning Center" in titles
    assert "Druva Cloud Platform" in titles
    assert "Enterprise Workloads" in titles
    # None of the titles should end with "articles" (from the article-count span)
    for title in titles:
        assert not title.lower().endswith("articles"), (
            f"Title contains article count: {repr(title)}"
        )
    # None should contain the description text fragment
    for title in titles:
        assert "hub for tutorials" not in title.lower(), (
            f"Title contains description: {repr(title)}"
        )


@pytest.mark.asyncio
async def test_build_toc_articles_under_collection():
    """Articles scraped from a real collection page must be level 1, is_article=True."""
    toc = await IntercomProfile().build_toc(ROOT, _make_scraper())
    druva_col = next(e for e in toc if e.url == COLLECTION_URL)
    articles = [e for e in toc if e.parent_url == COLLECTION_URL]
    assert len(articles) > 0
    assert all(e.level == 1 for e in articles)
    assert all(e.is_article for e in articles)


@pytest.mark.asyncio
async def test_build_toc_article_clean_titles():
    """Article titles must NOT contain the description text."""
    toc = await IntercomProfile().build_toc(ROOT, _make_scraper())
    articles = [e for e in toc if e.parent_url == COLLECTION_URL]
    titles = [e.title for e in articles]
    assert "Release Notes - Cloud Platform" in titles
    assert "Log in to Druva Cloud Platform Console" in titles
    # Titles should not bleed into the description
    for title in titles:
        assert len(title) < 200, f"Title suspiciously long (desc bleed?): {repr(title[:200])}"


@pytest.mark.asyncio
async def test_build_toc_article_urls_contain_articles_path():
    toc = await IntercomProfile().build_toc(ROOT, _make_scraper())
    articles = [e for e in toc if e.is_article]
    assert all("/articles/" in e.url for e in articles)


@pytest.mark.asyncio
async def test_build_toc_dom_order_preserved():
    """Collections must appear in the same order as in the HTML."""
    toc = await IntercomProfile().build_toc(ROOT, _make_scraper())
    collections = [e for e in toc if e.level == 0]
    titles = [e.title for e in collections]
    # From the fixture: Learning Center is first, Druva Cloud Platform second
    assert titles.index("Learning Center") < titles.index("Druva Cloud Platform")
    assert titles.index("Druva Cloud Platform") < titles.index("Enterprise Workloads")
