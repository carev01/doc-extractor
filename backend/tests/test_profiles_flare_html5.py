"""Tests for the MadCap Flare HTML5 (Side Navigation) extraction profile.

The fixture is a snapshot of:
  https://saasprotection.datto.com/help/M365/Content/M365_Home.htm
  (Datto/Kaseya M365 SaaS Protection documentation)

Key invariants verified:
  - detect() returns True only for the HTML5 Side Nav skin.
  - detect() returns False for the frame-based WebHelp/TriPane variant (the
    critical same-vendor collision guard) and for all other platform fixtures.
  - build_toc() returns a non-empty ordered list with clean, non-duplicated
    titles extracted from the static page.
  - content_config() targets [data-mc-content-body].
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.profiles.flare_html5 import FlareHtml5Profile
from app.services.profiles.scraper import FakeScraper

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "platforms")

# The Datto M365 MadCap Flare HTML5 Side Navigation fixture
FLARE_HTML5_FIXTURE = os.path.join(FIXTURE_DIR, "flare_html5.html")
# The Arcserve MadCap Flare WebHelp / TriPane fixture (frame-based)
FLARE_WEBHELP_FIXTURE = os.path.join(FIXTURE_DIR, "flare_webhelp.html")
# Other platform fixtures (must all be rejected)
LAZY_TREE_FIXTURE = os.path.join(FIXTURE_DIR, "lazy_tree.html")
DOCUSAURUS_FIXTURE = os.path.join(FIXTURE_DIR, "docusaurus.html")
MKDOCS_FIXTURE = os.path.join(FIXTURE_DIR, "mkdocs.html")
GITBOOK_FIXTURE = os.path.join(FIXTURE_DIR, "gitbook.html")

ROOT = "https://saasprotection.datto.com/help/M365/Content/M365_Home.htm"


def _read(path: str) -> str:
    return open(path, encoding="utf-8").read()


# ---------------------------------------------------------------------------
# Detection — positive
# ---------------------------------------------------------------------------

def test_detect_matches_flare_html5():
    assert FlareHtml5Profile().detect(_read(FLARE_HTML5_FIXTURE), ROOT) is True


# ---------------------------------------------------------------------------
# Detection — negative (collision guards)
# ---------------------------------------------------------------------------

def test_detect_rejects_flare_webhelp():
    """Critical: the two Flare variants must not collide.

    flare_webhelp.html is the frame-based WebHelp/TriPane skin (Arcserve UDP).
    It does not contain 'sidenav' at all, so the guard fires cleanly.
    """
    assert FlareHtml5Profile().detect(_read(FLARE_WEBHELP_FIXTURE), ROOT) is False


def test_detect_rejects_lazy_tree():
    assert FlareHtml5Profile().detect(_read(LAZY_TREE_FIXTURE), ROOT) is False


def test_detect_rejects_docusaurus():
    assert FlareHtml5Profile().detect(_read(DOCUSAURUS_FIXTURE), ROOT) is False


def test_detect_rejects_mkdocs():
    assert FlareHtml5Profile().detect(_read(MKDOCS_FIXTURE), ROOT) is False


def test_detect_rejects_gitbook():
    assert FlareHtml5Profile().detect(_read(GITBOOK_FIXTURE), ROOT) is False


# ---------------------------------------------------------------------------
# Content config
# ---------------------------------------------------------------------------

def test_content_config_include_tags():
    cfg = FlareHtml5Profile().content_config()
    assert cfg["includeTags"] == ["[data-mc-content-body]"]


def test_content_config_excludes_skin_chrome():
    cfg = FlareHtml5Profile().content_config()
    assert cfg["excludeTags"] == [".GoToTop", ".feedback-button", ".nocontent"]


def test_content_config_only_main_content_false():
    cfg = FlareHtml5Profile().content_config()
    assert cfg["onlyMainContent"] is False


def test_content_config_wait_for():
    cfg = FlareHtml5Profile().content_config()
    assert cfg["waitFor"] == 1500


# ---------------------------------------------------------------------------
# TOC building
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_build_toc_is_non_empty():
    toc = await FlareHtml5Profile().build_toc(
        ROOT, FakeScraper({ROOT: _read(FLARE_HTML5_FIXTURE)})
    )
    assert len(toc) > 0


@pytest.mark.asyncio
async def test_build_toc_known_top_level_titles():
    """Spot-check the three top-level items visible in the static fixture."""
    toc = await FlareHtml5Profile().build_toc(
        ROOT, FakeScraper({ROOT: _read(FLARE_HTML5_FIXTURE)})
    )
    titles = [e.title for e in toc]
    assert "HOME" in titles
    assert "RELEASE NOTES" in titles
    assert "M365 ONLINE HELP" in titles


@pytest.mark.asyncio
async def test_build_toc_no_duplicate_titles_from_invisible_label():
    """Flare injects <span class='invisible-label'> inside <a> tags; titles
    must not be doubled (e.g. 'M365 ONLINE HELPM365 ONLINE HELP')."""
    toc = await FlareHtml5Profile().build_toc(
        ROOT, FakeScraper({ROOT: _read(FLARE_HTML5_FIXTURE)})
    )
    for entry in toc:
        assert entry.title == entry.title.strip(), "Title has leading/trailing whitespace"
        # A duplicated title would be at least twice as long as its clean form
        # (e.g. "HOME" doubled = "HOMEHOME"). We check no title is a repetition.
        half = len(entry.title) // 2
        if half > 0:
            assert entry.title[:half] != entry.title[half:], (
                f"Title appears to be duplicated: {entry.title!r}"
            )


@pytest.mark.asyncio
async def test_build_toc_dom_order_preserved():
    """HOME must appear before RELEASE NOTES, which must appear before M365 ONLINE HELP."""
    toc = await FlareHtml5Profile().build_toc(
        ROOT, FakeScraper({ROOT: _read(FLARE_HTML5_FIXTURE)})
    )
    titles = [e.title for e in toc]
    assert titles.index("HOME") < titles.index("RELEASE NOTES")
    assert titles.index("RELEASE NOTES") < titles.index("M365 ONLINE HELP")


@pytest.mark.asyncio
async def test_build_toc_nested_entries_present():
    """The fixture has the M365 sub-menu statically expanded; level-1 entries
    should be present."""
    toc = await FlareHtml5Profile().build_toc(
        ROOT, FakeScraper({ROOT: _read(FLARE_HTML5_FIXTURE)})
    )
    level1 = [e for e in toc if e.level == 1]
    assert len(level1) >= 1, "Expected at least one level-1 nested entry"
    # Spot-check a known sub-entry
    titles = [e.title for e in level1]
    assert "Administrator requirements" in titles


@pytest.mark.asyncio
async def test_build_toc_entries_have_non_empty_titles_and_urls():
    toc = await FlareHtml5Profile().build_toc(
        ROOT, FakeScraper({ROOT: _read(FLARE_HTML5_FIXTURE)})
    )
    for entry in toc:
        assert entry.title, f"Empty title: {entry}"
        if entry.is_article:
            assert entry.url, f"Article entry has empty url: {entry}"
