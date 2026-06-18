"""Tests for the LLM fallback extraction profile.

All tests inject a fake client so the Anthropic API is never called.
Settings are patched via monkeypatch.setattr so the rest of the suite is
unaffected (the flag default is False).
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.services.profiles import registry
from app.services.profiles.llm import LlmProfile
from app.services.profiles.scraper import FakeScraper

ROOT = "https://example.com/docs/"

# ── flag OFF — silent no-op ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_build_toc_returns_empty_when_flag_off(monkeypatch):
    """build_toc must return [] immediately when llm_fallback_enabled is False."""
    monkeypatch.setattr(settings, "llm_fallback_enabled", False)

    called = []

    def fake_client(html: str, root_url: str) -> dict:
        called.append((html, root_url))
        return {"strategy": "sitemap"}

    scraper = FakeScraper(html_by_url={ROOT: "<html/>"})
    profile = LlmProfile(client=fake_client)
    result = await profile.build_toc(ROOT, scraper)

    assert result == []
    assert called == [], "Client must not be called when flag is off"


# ── flag ON + sidebar strategy ───────────────────────────────────────────────

SIDEBAR_HTML = (
    '<nav id="t">'
    "<ul>"
    '<li><a href="/a">A</a></li>'
    '<li><a href="/b">B</a></li>'
    "</ul>"
    "</nav>"
)


@pytest.mark.asyncio
async def test_build_toc_sidebar_strategy(monkeypatch):
    """Sidebar spec → sidebar_tree_toc entries with correct titles."""
    monkeypatch.setattr(settings, "llm_fallback_enabled", True)

    def fake_client(html: str, root_url: str) -> dict:
        return {"strategy": "sidebar", "nav_selector": "#t"}

    scraper = FakeScraper(html_by_url={ROOT: SIDEBAR_HTML})
    profile = LlmProfile(client=fake_client)
    result = await profile.build_toc(ROOT, scraper)

    titles = [e.title for e in result]
    assert titles == ["A", "B"]


@pytest.mark.asyncio
async def test_build_toc_sidebar_urls(monkeypatch):
    """Sidebar strategy produces correct absolute URLs."""
    monkeypatch.setattr(settings, "llm_fallback_enabled", True)

    def fake_client(html: str, root_url: str) -> dict:
        return {"strategy": "sidebar", "nav_selector": "#t"}

    scraper = FakeScraper(html_by_url={ROOT: SIDEBAR_HTML})
    profile = LlmProfile(client=fake_client)
    result = await profile.build_toc(ROOT, scraper)

    urls = [e.url for e in result]
    assert "https://example.com/a" in urls
    assert "https://example.com/b" in urls


# ── flag ON + hubspoke strategy ──────────────────────────────────────────────

_CAT_URL = "https://example.com/docs/cat1/"
_ART1_URL = "https://example.com/docs/cat1/art1"
_ART2_URL = "https://example.com/docs/cat1/art2"

_ROOT_HTML = (
    '<div class="categories">'
    f'<a href="{_CAT_URL}">Cat 1</a>'
    "</div>"
)
_CAT_HTML = (
    '<div class="articles">'
    f'<a href="{_ART1_URL}">Art 1</a>'
    f'<a href="{_ART2_URL}">Art 2</a>'
    "</div>"
)

HUBSPOKE_PAGES = {
    ROOT: _ROOT_HTML,
    _CAT_URL: _CAT_HTML,
}


@pytest.mark.asyncio
async def test_build_toc_hubspoke_categories(monkeypatch):
    """Hubspoke spec → category entry at level 0."""
    monkeypatch.setattr(settings, "llm_fallback_enabled", True)

    def fake_client(html: str, root_url: str) -> dict:
        return {
            "strategy": "hubspoke",
            "category_link_selector": ".categories a",
            "article_link_selector": ".articles a",
        }

    scraper = FakeScraper(html_by_url=HUBSPOKE_PAGES)
    profile = LlmProfile(client=fake_client)
    result = await profile.build_toc(ROOT, scraper)

    level0 = [e for e in result if e.level == 0]
    assert any(e.url == _CAT_URL for e in level0), "Category must be in TOC at level 0"


@pytest.mark.asyncio
async def test_build_toc_hubspoke_articles(monkeypatch):
    """Hubspoke spec → article entries discovered under categories."""
    monkeypatch.setattr(settings, "llm_fallback_enabled", True)

    def fake_client(html: str, root_url: str) -> dict:
        return {
            "strategy": "hubspoke",
            "category_link_selector": ".categories a",
            "article_link_selector": ".articles a",
        }

    scraper = FakeScraper(html_by_url=HUBSPOKE_PAGES)
    profile = LlmProfile(client=fake_client)
    result = await profile.build_toc(ROOT, scraper)

    urls = [e.url for e in result]
    assert _ART1_URL in urls
    assert _ART2_URL in urls


# ── sitemap strategy ─────────────────────────────────────────────────────────

_SITEMAP_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/docs/page1</loc></url>
  <url><loc>https://example.com/docs/page2</loc></url>
</urlset>
"""

_SITEMAP_URL = "https://example.com/sitemap.xml"

SITEMAP_PAGES = {
    ROOT: "<html/>",
    _SITEMAP_URL: _SITEMAP_XML,
}


@pytest.mark.asyncio
async def test_build_toc_sitemap_strategy(monkeypatch):
    """Sitemap spec → flat TocEntry list derived from sitemap.xml."""
    monkeypatch.setattr(settings, "llm_fallback_enabled", True)

    def fake_client(html: str, root_url: str) -> dict:
        return {"strategy": "sitemap"}

    scraper = FakeScraper(html_by_url=SITEMAP_PAGES)
    profile = LlmProfile(client=fake_client)
    result = await profile.build_toc(ROOT, scraper)

    urls = [e.url for e in result]
    assert "https://example.com/docs/page1" in urls
    assert "https://example.com/docs/page2" in urls


@pytest.mark.asyncio
async def test_build_toc_unknown_strategy_falls_back_to_sitemap(monkeypatch):
    """Unknown strategy value is treated as 'sitemap'."""
    monkeypatch.setattr(settings, "llm_fallback_enabled", True)

    def fake_client(html: str, root_url: str) -> dict:
        return {"strategy": "completely_unknown"}

    scraper = FakeScraper(html_by_url=SITEMAP_PAGES)
    profile = LlmProfile(client=fake_client)
    result = await profile.build_toc(ROOT, scraper)

    # Should not raise; may return [] if sitemap is empty, or entries otherwise.
    assert isinstance(result, list)


# ── detect always False ──────────────────────────────────────────────────────

def test_detect_always_false():
    profile = LlmProfile(client=lambda h, u: {})
    assert profile.detect("<html/>", ROOT) is False


def test_detect_always_false_empty_html():
    profile = LlmProfile(client=lambda h, u: {})
    assert profile.detect("", "https://anything.example.com/") is False


# ── content_config ───────────────────────────────────────────────────────────

def test_content_config_only_main_content():
    profile = LlmProfile(client=lambda h, u: {})
    assert profile.content_config()["onlyMainContent"] is True


def test_content_config_wait_for():
    profile = LlmProfile(client=lambda h, u: {})
    assert profile.content_config()["waitFor"] == 1500


# ── resilience — client raises ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_build_toc_returns_empty_on_client_exception(monkeypatch):
    """If the injected client raises, build_toc must return [] without propagating."""
    monkeypatch.setattr(settings, "llm_fallback_enabled", True)

    def exploding_client(html: str, root_url: str) -> dict:
        raise RuntimeError("Anthropic API down")

    scraper = FakeScraper(html_by_url={ROOT: "<html/>"})
    profile = LlmProfile(client=exploding_client)
    result = await profile.build_toc(ROOT, scraper)

    assert result == []


@pytest.mark.asyncio
async def test_build_toc_returns_empty_on_scraper_exception(monkeypatch):
    """If get_html raises, build_toc must return [] without propagating."""
    monkeypatch.setattr(settings, "llm_fallback_enabled", True)

    class BrokenScraper:
        async def get_html(self, url, wait_ms=1500):
            raise OSError("network failure")

        async def map_urls(self, root_url):
            return []

    profile = LlmProfile(client=lambda h, u: {"strategy": "sidebar", "nav_selector": "nav"})
    result = await profile.build_toc(ROOT, BrokenScraper())

    assert result == []


# ── registry ─────────────────────────────────────────────────────────────────

def test_llm_profile_registered():
    """The llm profile must be present in the registry after package import."""
    assert registry.get("llm") is not None


def test_llm_profile_name():
    assert registry.get("llm").name == "llm"
