"""Tests for the Freshdesk help-center extraction profile (3-level hub-and-spoke)."""

import os
import sys

import pytest
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.profiles.freshdesk import FreshdeskProfile
from app.services.profiles.scraper import FakeScraper

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "platforms")

ROOT = "https://help.keepit.com/support/home"
# Microsoft 365 category URL (from home fixture)
CATEGORY_URL = "https://help.keepit.com/support/solutions/6000139060"
# "Get Started" folder URL (from category fixture)
FOLDER_URL = "https://help.keepit.com/support/solutions/folders/6000236607"


def _read(name: str) -> str:
    return open(os.path.join(FIXTURE_DIR, name), encoding="utf-8").read()


# ---------------------------------------------------------------------------
# detect() — True on freshdesk fixture, False on all others
# ---------------------------------------------------------------------------

def test_detect_true_on_freshdesk_fixture():
    assert FreshdeskProfile().detect(_read("freshdesk.html"), ROOT) is True


def test_detect_false_on_commvault():
    assert FreshdeskProfile().detect(_read("commvault.html"), "https://documentation.commvault.com/") is False


def test_detect_false_on_docusaurus():
    assert FreshdeskProfile().detect(_read("docusaurus.html"), "https://docs.portworx.com/") is False


def test_detect_false_on_mkdocs():
    assert FreshdeskProfile().detect(_read("mkdocs.html"), "https://docs.example.com/") is False


def test_detect_false_on_gitbook():
    assert FreshdeskProfile().detect(_read("gitbook.html"), "https://docs.example.com/") is False


def test_detect_false_on_flare_html5():
    assert FreshdeskProfile().detect(_read("flare_html5.html"), "https://docs.example.com/") is False


def test_detect_false_on_flare_webhelp():
    assert FreshdeskProfile().detect(_read("flare_webhelp.html"), "https://docs.example.com/") is False


def test_detect_false_on_intercom():
    assert FreshdeskProfile().detect(_read("intercom.html"), "https://help.druva.com/en/") is False


# ---------------------------------------------------------------------------
# content_config()
# ---------------------------------------------------------------------------

def test_content_config():
    cfg = FreshdeskProfile().content_config()
    assert cfg["onlyMainContent"] is True
    assert cfg["waitFor"] == 1500


# ---------------------------------------------------------------------------
# build_toc() — offline via FakeScraper
#
# We serve:
#   home  → freshdesk.html  (9 category links + 1 folder link on home)
#   CATEGORY_URL → freshdesk_category.html  (4 unique folder links, 12 articles)
#   FOLDER_URL   → freshdesk_folder.html    (10 unique article links)
#   All other category/folder URLs → minimal stub (empty body)
# ---------------------------------------------------------------------------

def _make_scraper() -> FakeScraper:
    home_html = _read("freshdesk.html")
    category_html = _read("freshdesk_category.html")
    folder_html = _read("freshdesk_folder.html")
    stub = "<html><body></body></html>"

    # Enumerate all category hrefs from home to pre-populate the map
    soup = BeautifulSoup(home_html, "html.parser")
    cat_sel = (
        'a[href*="/support/solutions/"]'
        ':not([href*="/solutions/folders/"])'
        ':not([href*="/solutions/articles/"])'
    )
    folder_sel = 'a[href*="/support/solutions/folders/"]'

    html_by_url: dict[str, str] = {ROOT: home_html}

    # Map category pages
    for a in soup.select(cat_sel):
        href = a.get("href", "")
        if not href:
            continue
        if href == CATEGORY_URL:
            html_by_url[href] = category_html
        else:
            html_by_url[href] = stub

    # The home page also has a folder link (Partners) — serve stub for it
    for a in soup.select(folder_sel):
        href = a.get("href", "")
        if href and href not in html_by_url:
            html_by_url[href] = stub

    # Enumerate folder hrefs from the category page
    cat_soup = BeautifulSoup(category_html, "html.parser")
    seen_folders: set[str] = set()
    for a in cat_soup.select(folder_sel):
        href = a.get("href", "")
        if not href or href in seen_folders:
            continue
        seen_folders.add(href)
        if href == FOLDER_URL:
            html_by_url[href] = folder_html
        else:
            html_by_url[href] = stub

    return FakeScraper(html_by_url)


@pytest.mark.asyncio
async def test_build_toc_categories_at_level_0():
    toc = await FreshdeskProfile().build_toc(ROOT, _make_scraper())
    categories = [e for e in toc if e.level == 0]
    # Home page has 9 category links; the Partners folder on home is NOT a
    # category (it's /solutions/folders/…) so it's excluded by the selector.
    assert len(categories) == 9
    assert all(not e.is_article for e in categories)


@pytest.mark.asyncio
async def test_build_toc_category_titles():
    toc = await FreshdeskProfile().build_toc(ROOT, _make_scraper())
    categories = [e for e in toc if e.level == 0]
    titles = [e.title for e in categories]
    assert "Keepit Platform" in titles
    assert "Microsoft 365" in titles
    assert "Google Workspace" in titles
    assert "Salesforce" in titles


@pytest.mark.asyncio
async def test_build_toc_folders_at_level_1():
    toc = await FreshdeskProfile().build_toc(ROOT, _make_scraper())
    folders = [e for e in toc if e.level == 1]
    # Microsoft 365 category has 4 unique folders; all other categories return
    # stubs (0 folders). Partners folder on home is processed via its stub too.
    assert len(folders) == 4
    assert all(not e.is_article for e in folders)


@pytest.mark.asyncio
async def test_build_toc_folder_titles():
    toc = await FreshdeskProfile().build_toc(ROOT, _make_scraper())
    folders = [e for e in toc if e.level == 1]
    titles = [e.title for e in folders]
    assert "Get Started" in titles
    assert "Your Microsoft 365 Backup" in titles
    assert "Recover Your Data" in titles
    assert "Troubleshooting" in titles


@pytest.mark.asyncio
async def test_build_toc_articles_at_level_2():
    toc = await FreshdeskProfile().build_toc(ROOT, _make_scraper())
    articles = [e for e in toc if e.level == 2]
    # "Get Started" folder has 10 unique articles; other folders return stubs.
    assert len(articles) == 10
    assert all(e.is_article for e in articles)


@pytest.mark.asyncio
async def test_build_toc_article_titles():
    toc = await FreshdeskProfile().build_toc(ROOT, _make_scraper())
    articles = [e for e in toc if e.level == 2]
    titles = [e.title for e in articles]
    assert "Prepare your Microsoft 365 account" in titles
    assert "Create a Microsoft 365 connector" in titles
    assert "Configure your Microsoft 365 backup" in titles


@pytest.mark.asyncio
async def test_build_toc_hierarchy_links():
    """Folders must point to a category; articles must point to a folder."""
    toc = await FreshdeskProfile().build_toc(ROOT, _make_scraper())
    cat_urls = {e.url for e in toc if e.level == 0}
    folder_urls = {e.url for e in toc if e.level == 1}

    for e in toc:
        if e.level == 1:
            assert e.parent_url in cat_urls, f"Folder parent not a category: {e}"
        elif e.level == 2:
            assert e.parent_url in folder_urls, f"Article parent not a folder: {e}"


@pytest.mark.asyncio
async def test_build_toc_article_urls_contain_articles_path():
    toc = await FreshdeskProfile().build_toc(ROOT, _make_scraper())
    articles = [e for e in toc if e.is_article]
    assert all("/solutions/articles/" in e.url for e in articles)


@pytest.mark.asyncio
async def test_build_toc_dom_order_preserved():
    """Categories must appear in the same DOM order as on the home page."""
    toc = await FreshdeskProfile().build_toc(ROOT, _make_scraper())
    categories = [e for e in toc if e.level == 0]
    titles = [e.title for e in categories]
    # From the fixture: Keepit Platform is first, Microsoft 365 is second
    assert titles.index("Keepit Platform") < titles.index("Microsoft 365")
    assert titles.index("Microsoft 365") < titles.index("Entra ID")
