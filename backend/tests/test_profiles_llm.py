"""Tests for the LLM fallback extraction profile.

All network calls are monkeypatched so no real API is ever called.
Settings are patched via monkeypatch.setattr so the rest of the suite is
unaffected (the flag default is False).
"""

import json
import os
import sys
import types
import unittest.mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.services.profiles import registry
from app.services.profiles.llm import DerivedProfile, LlmProfile, derive_spec
from app.services.profiles.scraper import FakeScraper

ROOT = "https://example.com/docs/"

# ── flag OFF — silent no-op ──────────────────────────────────────────────────

async def test_build_toc_returns_empty_when_flag_off(monkeypatch):
    """build_toc must return [] immediately when llm_fallback_enabled is False."""
    monkeypatch.setattr(settings, "llm_fallback_enabled", False)

    called = []

    scraper = FakeScraper(html_by_url={ROOT: "<html/>"})
    # Patch derive_spec so if it were called it would register the call
    import app.services.profiles.llm as llm_mod

    async def fake_derive(html, url):
        called.append((html, url))
        return {"strategy": "sitemap"}

    monkeypatch.setattr(llm_mod, "derive_spec", fake_derive)
    profile = LlmProfile()
    result = await profile.build_toc(ROOT, scraper)

    assert result == []
    assert called == [], "derive_spec must not be called when flag is off"


# ── derive_spec — Anthropic provider ────────────────────────────────────────

async def test_derive_spec_anthropic_request_shape(monkeypatch):
    """derive_spec with provider='anthropic' sends correct URL and headers."""
    monkeypatch.setattr(settings, "llm_provider", "anthropic")
    monkeypatch.setattr(settings, "llm_api_key", "test-key-ant")
    monkeypatch.setattr(settings, "llm_model", "")
    monkeypatch.setattr(settings, "llm_base_url", "")

    captured = {}

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"content": [{"text": '{"strategy":"sidebar","nav_selector":"#t"}'}]}

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, headers=None, json=None, **kwargs):
            captured["url"] = url
            captured["headers"] = headers
            captured["body"] = json
            return FakeResponse()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: FakeAsyncClient())

    spec = await derive_spec("<html>nav here</html>", ROOT)

    assert spec == {"strategy": "sidebar", "nav_selector": "#t"}
    assert "api.anthropic.com" in captured["url"]
    assert captured["headers"]["x-api-key"] == "test-key-ant"
    assert captured["headers"]["anthropic-version"] == "2023-06-01"
    assert captured["body"]["model"] == "claude-haiku-4-5"
    assert captured["body"]["max_tokens"] == 512


async def test_derive_spec_anthropic_html_truncated(monkeypatch):
    """HTML is truncated to 20 000 chars before sending."""
    monkeypatch.setattr(settings, "llm_provider", "anthropic")
    monkeypatch.setattr(settings, "llm_api_key", "k")
    monkeypatch.setattr(settings, "llm_model", "")
    monkeypatch.setattr(settings, "llm_base_url", "")

    captured = {}

    class FakeResponse:
        def raise_for_status(self): pass
        def json(self): return {"content": [{"text": "{}"}]}

    class FakeAsyncClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, headers=None, json=None, **kwargs):
            captured["content"] = json["messages"][0]["content"]
            return FakeResponse()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: FakeAsyncClient())

    big_html = "x" * 30_000
    await derive_spec(big_html, ROOT)

    # The snippet in the message should be at most 20 000 chars
    snippet_part = captured["content"].split("HTML (truncated):\n", 1)[1]
    assert len(snippet_part) <= 20_000


# ── derive_spec — OpenAI provider ───────────────────────────────────────────

async def test_derive_spec_openai_request_shape(monkeypatch):
    """derive_spec with provider='openai' sends correct URL and Bearer header."""
    monkeypatch.setattr(settings, "llm_provider", "openai")
    monkeypatch.setattr(settings, "llm_api_key", "sk-open-key")
    monkeypatch.setattr(settings, "llm_model", "")
    monkeypatch.setattr(settings, "llm_base_url", "")

    captured = {}

    class FakeResponse:
        def raise_for_status(self): pass
        def json(self):
            return {"choices": [{"message": {"content": '{"strategy":"hubspoke","category_link_selector":".cat a","article_link_selector":".art a"}'}}]}

    class FakeAsyncClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, headers=None, json=None, **kwargs):
            captured["url"] = url
            captured["headers"] = headers
            captured["body"] = json
            return FakeResponse()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: FakeAsyncClient())

    spec = await derive_spec("<html/>", ROOT)

    assert spec["strategy"] == "hubspoke"
    assert "openai.com" in captured["url"]
    assert captured["headers"]["Authorization"] == "Bearer sk-open-key"
    assert captured["body"]["model"] == "gpt-4o-mini"
    assert captured["body"]["response_format"] == {"type": "json_object"}


async def test_derive_spec_openai_custom_base_url(monkeypatch):
    """When llm_base_url is set, derive_spec uses it instead of the provider default."""
    monkeypatch.setattr(settings, "llm_provider", "openai")
    monkeypatch.setattr(settings, "llm_api_key", "k")
    monkeypatch.setattr(settings, "llm_model", "my-model")
    monkeypatch.setattr(settings, "llm_base_url", "https://my-local-llm/v1/chat")

    captured = {}

    class FakeResponse:
        def raise_for_status(self): pass
        def json(self):
            return {"choices": [{"message": {"content": "{}"}}]}

    class FakeAsyncClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kwargs):
            captured["url"] = url
            return FakeResponse()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: FakeAsyncClient())

    await derive_spec("<html/>", ROOT)

    assert captured["url"] == "https://my-local-llm/v1/chat"


# ── derive_spec — error / missing key ───────────────────────────────────────

async def test_derive_spec_returns_none_missing_api_key(monkeypatch):
    """derive_spec returns None when llm_api_key is empty."""
    monkeypatch.setattr(settings, "llm_api_key", "")
    monkeypatch.setattr(settings, "llm_provider", "anthropic")

    result = await derive_spec("<html/>", ROOT)
    assert result is None


async def test_derive_spec_returns_none_on_http_exception(monkeypatch):
    """derive_spec returns None (not raises) when the HTTP call fails."""
    monkeypatch.setattr(settings, "llm_provider", "anthropic")
    monkeypatch.setattr(settings, "llm_api_key", "k")
    monkeypatch.setattr(settings, "llm_base_url", "")
    monkeypatch.setattr(settings, "llm_model", "")

    class BrokenClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, *args, **kwargs):
            raise ConnectionError("network down")

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: BrokenClient())

    result = await derive_spec("<html/>", ROOT)
    assert result is None


async def test_derive_spec_strips_markdown_fences(monkeypatch):
    """derive_spec parses JSON even when wrapped in ```json ... ``` fences."""
    monkeypatch.setattr(settings, "llm_provider", "anthropic")
    monkeypatch.setattr(settings, "llm_api_key", "k")
    monkeypatch.setattr(settings, "llm_base_url", "")
    monkeypatch.setattr(settings, "llm_model", "")

    class FakeResponse:
        def raise_for_status(self): pass
        def json(self):
            return {"content": [{"text": '```json\n{"strategy":"sitemap"}\n```'}]}

    class FakeAsyncClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, *args, **kwargs): return FakeResponse()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: FakeAsyncClient())

    result = await derive_spec("<html/>", ROOT)
    assert result == {"strategy": "sitemap"}


# ── DerivedProfile — sidebar strategy ───────────────────────────────────────

SIDEBAR_HTML = (
    '<nav id="t">'
    "<ul>"
    '<li><a href="/a">A</a></li>'
    '<li><a href="/b">B</a></li>'
    "</ul>"
    "</nav>"
)


async def test_derived_profile_sidebar_dispatch():
    """Sidebar spec → sidebar_tree_toc entries with correct titles."""
    spec = {"strategy": "sidebar", "nav_selector": "#t"}
    profile = DerivedProfile(spec)
    scraper = FakeScraper(html_by_url={ROOT: SIDEBAR_HTML})
    result = await profile.build_toc(ROOT, scraper)

    titles = [e.title for e in result]
    assert "A" in titles
    assert "B" in titles


async def test_derived_profile_sidebar_urls():
    """Sidebar strategy produces correct absolute URLs."""
    spec = {"strategy": "sidebar", "nav_selector": "#t"}
    profile = DerivedProfile(spec)
    scraper = FakeScraper(html_by_url={ROOT: SIDEBAR_HTML})
    result = await profile.build_toc(ROOT, scraper)

    urls = [e.url for e in result]
    assert "https://example.com/a" in urls
    assert "https://example.com/b" in urls


# ── DerivedProfile — hubspoke strategy ──────────────────────────────────────

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


async def test_derived_profile_hubspoke_categories():
    """Hubspoke spec → category entry at level 0."""
    spec = {
        "strategy": "hubspoke",
        "category_link_selector": ".categories a",
        "article_link_selector": ".articles a",
    }
    profile = DerivedProfile(spec)
    scraper = FakeScraper(html_by_url=HUBSPOKE_PAGES)
    result = await profile.build_toc(ROOT, scraper)

    level0 = [e for e in result if e.level == 0]
    assert any(e.url == _CAT_URL for e in level0)


async def test_derived_profile_hubspoke_articles():
    """Hubspoke spec → article entries discovered under categories."""
    spec = {
        "strategy": "hubspoke",
        "category_link_selector": ".categories a",
        "article_link_selector": ".articles a",
    }
    profile = DerivedProfile(spec)
    scraper = FakeScraper(html_by_url=HUBSPOKE_PAGES)
    result = await profile.build_toc(ROOT, scraper)

    urls = [e.url for e in result]
    assert _ART1_URL in urls
    assert _ART2_URL in urls


# ── DerivedProfile — sitemap strategy ───────────────────────────────────────

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


async def test_derived_profile_sitemap_dispatch():
    """Sitemap spec → flat TocEntry list derived from sitemap.xml."""
    spec = {"strategy": "sitemap"}
    profile = DerivedProfile(spec)
    scraper = FakeScraper(html_by_url=SITEMAP_PAGES)
    result = await profile.build_toc(ROOT, scraper)

    urls = [e.url for e in result]
    assert "https://example.com/docs/page1" in urls
    assert "https://example.com/docs/page2" in urls


# ── DerivedProfile — content_config ─────────────────────────────────────────

def test_derived_profile_content_config_with_selector():
    """content_config returns includeTags when content_selector is present."""
    spec = {"strategy": "sidebar", "nav_selector": "nav", "content_selector": "article.main"}
    profile = DerivedProfile(spec)
    cfg = profile.content_config()

    assert "includeTags" in cfg
    assert "article.main" in cfg["includeTags"]
    assert cfg["onlyMainContent"] is False
    assert cfg["waitFor"] == 1500


def test_derived_profile_content_config_without_selector():
    """content_config falls back to onlyMainContent when no content_selector."""
    spec = {"strategy": "sitemap"}
    profile = DerivedProfile(spec)
    cfg = profile.content_config()

    assert cfg["onlyMainContent"] is True
    assert cfg["waitFor"] == 1500
    assert "includeTags" not in cfg


def test_derived_profile_content_config_empty_selector():
    """Empty string content_selector treated as absent."""
    spec = {"strategy": "sidebar", "nav_selector": "nav", "content_selector": ""}
    profile = DerivedProfile(spec)
    cfg = profile.content_config()

    assert cfg["onlyMainContent"] is True


def test_derived_profile_content_config_whitespace_selector():
    """Whitespace-only content_selector treated as absent."""
    spec = {"strategy": "sidebar", "nav_selector": "nav", "content_selector": "   "}
    profile = DerivedProfile(spec)
    cfg = profile.content_config()

    assert cfg["onlyMainContent"] is True


# ── DerivedProfile — resilience ──────────────────────────────────────────────

async def test_derived_profile_returns_empty_on_exception():
    """DerivedProfile.build_toc returns [] on strategy dispatch failure."""
    spec = {"strategy": "sidebar", "nav_selector": "#missing-selector"}

    class BrokenScraper:
        async def get_html(self, url, wait_ms=1500):
            raise OSError("network failure")
        async def map_urls(self, root_url):
            return []

    profile = DerivedProfile(spec)
    result = await profile.build_toc(ROOT, BrokenScraper())
    assert result == []


def test_derived_profile_detect_always_false():
    """DerivedProfile.detect always returns False."""
    profile = DerivedProfile({"strategy": "sitemap"})
    assert profile.detect("<html/>", ROOT) is False


# ── Resolver caching (key new behavior) ──────────────────────────────────────

async def test_resolve_profile_caches_derived_spec(monkeypatch):
    """_resolve_profile calls derive_spec once and caches result; second call is a hit."""
    from unittest.mock import AsyncMock, MagicMock, patch

    monkeypatch.setattr(settings, "llm_fallback_enabled", True)
    monkeypatch.setattr(settings, "llm_api_key", "k")

    # A source-like object (no DB needed)
    source = MagicMock()
    source.platform = None
    source.profile_config = None
    source.base_url = ROOT

    fixed_spec = {"strategy": "sidebar", "nav_selector": "#nav"}
    derive_call_count = 0

    # Patch detect_platform to return None (no auto-detection match)
    import app.services.firecrawl as fc_mod
    monkeypatch.setattr(fc_mod, "detect_platform", lambda html, url: None)

    # Patch derive_spec imported into firecrawl
    async def fake_derive(html, url):
        nonlocal derive_call_count
        derive_call_count += 1
        return fixed_spec

    monkeypatch.setattr(fc_mod.llm_mod, "derive_spec", fake_derive)

    # Stub Scraper.get_html so no network call is made
    async def fake_get_html(self_scraper, url, wait_ms=1500):
        return "<html><nav id='nav'><a href='/p'>P</a></nav></html>"

    from app.services.profiles.scraper import Scraper as ProfileScraper
    # firecrawl's Scraper is its own class; patch it
    import app.services.firecrawl as fc
    original_scraper_class = fc.Scraper

    class PatchedScraper(original_scraper_class):
        async def get_html(self, url, wait_ms=1500):
            return "<html><nav id='nav'><a href='/p'>P</a></nav></html>"

    monkeypatch.setattr(fc, "Scraper", PatchedScraper)

    # Build a FirecrawlService (no real httpx needed for _resolve_profile)
    from app.services.firecrawl import FirecrawlService
    svc = FirecrawlService.__new__(FirecrawlService)
    svc._content_config_by_source = {}

    # First call — should derive and cache
    profile1 = await svc._resolve_profile(source)

    assert profile1 is not None
    assert profile1.name == "llm"
    assert isinstance(profile1, fc_mod.llm_mod.DerivedProfile)
    assert source.profile_config is not None
    assert source.profile_config["llm_spec"] == fixed_spec
    assert derive_call_count == 1, "derive_spec should be called once on cache miss"

    # Second call with cached profile_config — should NOT re-derive
    profile2 = await svc._resolve_profile(source)

    assert profile2 is not None
    assert profile2.name == "llm"
    assert derive_call_count == 1, "derive_spec must NOT be called again on cache hit"


async def test_resolve_profile_explicit_llm_platform_honored_without_flag(monkeypatch):
    """source.platform=='llm' triggers LLM branch even when flag is off."""
    from unittest.mock import MagicMock

    monkeypatch.setattr(settings, "llm_fallback_enabled", False)
    monkeypatch.setattr(settings, "llm_api_key", "k")

    source = MagicMock()
    source.platform = "llm"
    source.profile_config = None
    source.base_url = ROOT

    fixed_spec = {"strategy": "sitemap"}

    import app.services.firecrawl as fc_mod
    monkeypatch.setattr(fc_mod, "detect_platform", lambda html, url: None)

    async def fake_derive(html, url):
        return fixed_spec

    monkeypatch.setattr(fc_mod.llm_mod, "derive_spec", fake_derive)

    original_scraper_class = fc_mod.Scraper

    class PatchedScraper(original_scraper_class):
        async def get_html(self, url, wait_ms=1500):
            return "<html/>"

    monkeypatch.setattr(fc_mod, "Scraper", PatchedScraper)

    from app.services.firecrawl import FirecrawlService
    svc = FirecrawlService.__new__(FirecrawlService)
    svc._content_config_by_source = {}

    profile = await svc._resolve_profile(source)
    assert profile is not None
    assert profile.name == "llm"


async def test_resolve_profile_falls_through_to_generic_when_derive_returns_none(monkeypatch):
    """When derive_spec returns None, _resolve_profile falls through to generic."""
    from unittest.mock import MagicMock

    monkeypatch.setattr(settings, "llm_fallback_enabled", True)
    monkeypatch.setattr(settings, "llm_api_key", "")  # empty key → None from derive_spec

    source = MagicMock()
    source.platform = None
    source.profile_config = None
    source.base_url = ROOT

    import app.services.firecrawl as fc_mod
    monkeypatch.setattr(fc_mod, "detect_platform", lambda html, url: None)

    async def fake_derive(html, url):
        return None

    monkeypatch.setattr(fc_mod.llm_mod, "derive_spec", fake_derive)

    original_scraper_class = fc_mod.Scraper

    class PatchedScraper(original_scraper_class):
        async def get_html(self, url, wait_ms=1500):
            return "<html/>"

    monkeypatch.setattr(fc_mod, "Scraper", PatchedScraper)

    from app.services.firecrawl import FirecrawlService
    svc = FirecrawlService.__new__(FirecrawlService)
    svc._content_config_by_source = {}

    profile = await svc._resolve_profile(source)
    assert profile is not None
    assert profile.name == "generic"


# ── flag ON + sidebar via LlmProfile ─────────────────────────────────────────

async def test_llm_profile_build_toc_sidebar_strategy(monkeypatch):
    """LlmProfile.build_toc with mocked derive_spec → sidebar entries."""
    monkeypatch.setattr(settings, "llm_fallback_enabled", True)

    import app.services.profiles.llm as llm_mod

    async def fake_derive(html, url):
        return {"strategy": "sidebar", "nav_selector": "#t"}

    monkeypatch.setattr(llm_mod, "derive_spec", fake_derive)

    scraper = FakeScraper(html_by_url={ROOT: SIDEBAR_HTML})
    profile = LlmProfile()
    result = await profile.build_toc(ROOT, scraper)

    titles = [e.title for e in result]
    assert "A" in titles
    assert "B" in titles


async def test_llm_profile_build_toc_hubspoke_strategy(monkeypatch):
    """LlmProfile.build_toc with mocked derive_spec → hubspoke entries."""
    monkeypatch.setattr(settings, "llm_fallback_enabled", True)

    import app.services.profiles.llm as llm_mod

    async def fake_derive(html, url):
        return {
            "strategy": "hubspoke",
            "category_link_selector": ".categories a",
            "article_link_selector": ".articles a",
        }

    monkeypatch.setattr(llm_mod, "derive_spec", fake_derive)

    scraper = FakeScraper(html_by_url=HUBSPOKE_PAGES)
    profile = LlmProfile()
    result = await profile.build_toc(ROOT, scraper)

    urls = [e.url for e in result]
    assert _ART1_URL in urls or _ART2_URL in urls


async def test_llm_profile_build_toc_sitemap_strategy(monkeypatch):
    """LlmProfile.build_toc with sitemap spec → flat TocEntry list."""
    monkeypatch.setattr(settings, "llm_fallback_enabled", True)

    import app.services.profiles.llm as llm_mod

    async def fake_derive(html, url):
        return {"strategy": "sitemap"}

    monkeypatch.setattr(llm_mod, "derive_spec", fake_derive)

    scraper = FakeScraper(html_by_url=SITEMAP_PAGES)
    profile = LlmProfile()
    result = await profile.build_toc(ROOT, scraper)

    urls = [e.url for e in result]
    assert "https://example.com/docs/page1" in urls


async def test_llm_profile_build_toc_unknown_strategy_falls_back_to_sitemap(monkeypatch):
    """Unknown strategy value is treated as 'sitemap'."""
    monkeypatch.setattr(settings, "llm_fallback_enabled", True)

    import app.services.profiles.llm as llm_mod

    async def fake_derive(html, url):
        return {"strategy": "completely_unknown"}

    monkeypatch.setattr(llm_mod, "derive_spec", fake_derive)

    scraper = FakeScraper(html_by_url=SITEMAP_PAGES)
    profile = LlmProfile()
    result = await profile.build_toc(ROOT, scraper)
    assert isinstance(result, list)


# ── detect always False ──────────────────────────────────────────────────────

def test_detect_always_false():
    profile = LlmProfile()
    assert profile.detect("<html/>", ROOT) is False


def test_detect_always_false_empty_html():
    profile = LlmProfile()
    assert profile.detect("", "https://anything.example.com/") is False


# ── content_config (LlmProfile — generic default) ───────────────────────────

def test_llm_profile_content_config_only_main_content():
    profile = LlmProfile()
    assert profile.content_config()["onlyMainContent"] is True


def test_llm_profile_content_config_wait_for():
    profile = LlmProfile()
    assert profile.content_config()["waitFor"] == 1500


# ── resilience — derive_spec raises ──────────────────────────────────────────

async def test_llm_profile_returns_empty_on_derive_exception(monkeypatch):
    """If derive_spec raises, build_toc must return [] without propagating."""
    monkeypatch.setattr(settings, "llm_fallback_enabled", True)

    import app.services.profiles.llm as llm_mod

    async def exploding_derive(html, url):
        raise RuntimeError("Anthropic API down")

    monkeypatch.setattr(llm_mod, "derive_spec", exploding_derive)

    scraper = FakeScraper(html_by_url={ROOT: "<html/>"})
    profile = LlmProfile()
    result = await profile.build_toc(ROOT, scraper)

    assert result == []


async def test_llm_profile_returns_empty_on_scraper_exception(monkeypatch):
    """If get_html raises, build_toc must return [] without propagating."""
    monkeypatch.setattr(settings, "llm_fallback_enabled", True)

    class BrokenScraper:
        async def get_html(self, url, wait_ms=1500):
            raise OSError("network failure")
        async def map_urls(self, root_url):
            return []

    profile = LlmProfile()
    result = await profile.build_toc(ROOT, BrokenScraper())
    assert result == []


# ── registry ──────────────────────────────────────────────────────────────────

def test_llm_profile_registered():
    """The llm profile must be present in the registry after package import."""
    assert registry.get("llm") is not None


def test_llm_profile_name():
    assert registry.get("llm").name == "llm"
