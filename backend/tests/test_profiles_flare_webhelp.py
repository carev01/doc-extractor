"""Tests for the MadCap Flare WebHelp / TriPane (frame-based) extraction profile.

The fixture is a Firecrawl-rendered snapshot of:
  https://documentation.arcserve.com/Arcserve-UDP/Available/10.0/ENU/Bookshelf_Files/HTML/SolG/default.htm
  (Arcserve UDP 10.0 Solutions Guide — the frame-based WebHelp skin).

TOC approach (see profile docstring): the canonical ``Data/Toc.xml`` is HTTP
404 on the live host even through Firecrawl, and the domain sitemap.xml carries
no URLs under the help-system path prefix.  The robust, deterministic source is
the inline ``<ul class="tree">`` that Flare renders into the index page, which
Firecrawl returns intact at the top level.  ``build_toc`` parses that tree, so
the test serves the index fixture via ``FakeScraper`` and asserts ordered
top-level entries with real titles and resolved topic URLs.

Key invariants verified:
  - detect() is True only for the frame-based WebHelp skin (MadCap + iframe).
  - detect() is False for the HTML5 Side Nav variant (same-vendor collision
    guard) and for all other platform fixtures.
  - build_toc() parses the inline tree into an ordered, non-empty TOC with
    clean titles, absolute topic URLs (hash + ?TocPath stripped), and
    leaf/section classification.
  - content_config() targets [data-mc-content-body].
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.profiles.flare_webhelp import FlareWebHelpProfile
from app.services.profiles.scraper import FakeScraper

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "platforms")

FLARE_WEBHELP_FIXTURE = os.path.join(FIXTURE_DIR, "flare_webhelp.html")
FLARE_HTML5_FIXTURE = os.path.join(FIXTURE_DIR, "flare_html5.html")
COMMVAULT_FIXTURE = os.path.join(FIXTURE_DIR, "commvault.html")
DOCUSAURUS_FIXTURE = os.path.join(FIXTURE_DIR, "docusaurus.html")
MKDOCS_FIXTURE = os.path.join(FIXTURE_DIR, "mkdocs.html")
GITBOOK_FIXTURE = os.path.join(FIXTURE_DIR, "gitbook.html")

ROOT = (
    "https://documentation.arcserve.com/Arcserve-UDP/Available/10.0/ENU/"
    "Bookshelf_Files/HTML/SolG/default.htm"
)
HELP_ROOT = (
    "https://documentation.arcserve.com/Arcserve-UDP/Available/10.0/ENU/"
    "Bookshelf_Files/HTML/SolG/"
)


def _read(path: str) -> str:
    return open(path, encoding="utf-8").read()


# ---------------------------------------------------------------------------
# Detection — positive
# ---------------------------------------------------------------------------

def test_detect_matches_flare_webhelp():
    assert FlareWebHelpProfile().detect(_read(FLARE_WEBHELP_FIXTURE), ROOT) is True


# ---------------------------------------------------------------------------
# Detection — negative (collision guards)
# ---------------------------------------------------------------------------

def test_detect_rejects_flare_html5():
    """Critical same-vendor guard: the HTML5 Side Nav skin has MadCap markers
    but no ``<iframe id="topic">``, so the frameset guard fires."""
    assert FlareWebHelpProfile().detect(_read(FLARE_HTML5_FIXTURE), ROOT) is False


def test_detect_rejects_commvault():
    assert FlareWebHelpProfile().detect(_read(COMMVAULT_FIXTURE), ROOT) is False


def test_detect_rejects_docusaurus():
    assert FlareWebHelpProfile().detect(_read(DOCUSAURUS_FIXTURE), ROOT) is False


def test_detect_rejects_mkdocs():
    assert FlareWebHelpProfile().detect(_read(MKDOCS_FIXTURE), ROOT) is False


def test_detect_rejects_gitbook():
    assert FlareWebHelpProfile().detect(_read(GITBOOK_FIXTURE), ROOT) is False


# ---------------------------------------------------------------------------
# Content config
# ---------------------------------------------------------------------------

def test_content_config_include_tags():
    cfg = FlareWebHelpProfile().content_config()
    assert cfg["includeTags"] == ["[data-mc-content-body]"]


def test_content_config_only_main_content_false():
    cfg = FlareWebHelpProfile().content_config()
    assert cfg["onlyMainContent"] is False


def test_content_config_wait_for():
    cfg = FlareWebHelpProfile().content_config()
    assert cfg["waitFor"] == 1500


# ---------------------------------------------------------------------------
# TOC building (inline <ul class="tree"> parse)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_build_toc_is_non_empty():
    toc = await FlareWebHelpProfile().build_toc(
        ROOT, FakeScraper({ROOT: _read(FLARE_WEBHELP_FIXTURE)})
    )
    assert len(toc) == 29  # all top-level tree nodes in the rendered fixture


@pytest.mark.asyncio
async def test_build_toc_known_titles_present():
    toc = await FlareWebHelpProfile().build_toc(
        ROOT, FakeScraper({ROOT: _read(FLARE_WEBHELP_FIXTURE)})
    )
    titles = [e.title for e in toc]
    assert "Solutions Guide" in titles
    assert "Understanding Arcserve UDP" in titles
    assert "Troubleshooting" in titles
    assert "Arcserve UDP Terms and Definitions" in titles


@pytest.mark.asyncio
async def test_build_toc_dom_order_preserved():
    toc = await FlareWebHelpProfile().build_toc(
        ROOT, FakeScraper({ROOT: _read(FLARE_WEBHELP_FIXTURE)})
    )
    titles = [e.title for e in toc]
    assert titles.index("Solutions Guide") < titles.index("Understanding Arcserve UDP")
    assert titles.index("Understanding Arcserve UDP") < titles.index("Troubleshooting")
    assert titles[0] == "Solutions Guide"
    assert titles[-1] == "Arcserve UDP Terms and Definitions"


@pytest.mark.asyncio
async def test_build_toc_resolves_hash_routed_urls():
    """Hash fragment becomes the topic path under the help root; the leading
    default.htm and the ?TocPath routing query are stripped."""
    toc = await FlareWebHelpProfile().build_toc(
        ROOT, FakeScraper({ROOT: _read(FLARE_WEBHELP_FIXTURE)})
    )
    by_title = {e.title: e for e in toc}
    assert by_title["Solutions Guide"].url == (
        HELP_ROOT + "UDPSolnGuide/title_page_udp_solutions_guide.htm"
    )
    # A fragment with no subdirectory resolves directly under the help root.
    assert by_title["Session Password Utility"].url == (
        HELP_ROOT + "Session_Password_Utility.htm"
    )
    for entry in toc:
        assert "#" not in entry.url
        assert "TocPath" not in entry.url
        assert entry.url.startswith(HELP_ROOT)


@pytest.mark.asyncio
async def test_build_toc_classifies_leaf_vs_section():
    """Leaf nodes (tree-node-leaf) are articles; collapsed nodes
    (tree-node-collapsed, lazy children) are sections."""
    toc = await FlareWebHelpProfile().build_toc(
        ROOT, FakeScraper({ROOT: _read(FLARE_WEBHELP_FIXTURE)})
    )
    by_title = {e.title: e for e in toc}
    # First four are leaves in the fixture.
    assert by_title["Solutions Guide"].is_article is True
    assert by_title["Legal Notices"].is_article is True
    # Collapsed chapters are sections.
    assert by_title["Understanding Arcserve UDP"].is_article is False
    assert by_title["Troubleshooting"].is_article is False


@pytest.mark.asyncio
async def test_build_toc_all_top_level():
    """Only the top-level tree is rendered (lazy nesting limitation)."""
    toc = await FlareWebHelpProfile().build_toc(
        ROOT, FakeScraper({ROOT: _read(FLARE_WEBHELP_FIXTURE)})
    )
    assert all(e.level == 0 for e in toc)
    assert all(e.parent_url is None for e in toc)


@pytest.mark.asyncio
async def test_build_toc_entries_have_clean_titles_and_urls():
    toc = await FlareWebHelpProfile().build_toc(
        ROOT, FakeScraper({ROOT: _read(FLARE_WEBHELP_FIXTURE)})
    )
    for entry in toc:
        assert entry.title and entry.title == entry.title.strip()
        assert entry.url


@pytest.mark.asyncio
async def test_build_toc_empty_when_no_tree():
    toc = await FlareWebHelpProfile().build_toc(
        ROOT, FakeScraper({ROOT: "<html><body>no toc here</body></html>"})
    )
    assert toc == []
