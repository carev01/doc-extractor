"""Tests for the GitBook extraction profile."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.profiles.gitbook import GitBookProfile
from app.services.profiles.scraper import FakeScraper

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "platforms")
GITBOOK_FIXTURE = os.path.join(FIXTURE_DIR, "gitbook.html")
COMMVAULT_FIXTURE = os.path.join(FIXTURE_DIR, "commvault.html")
DOCUSAURUS_FIXTURE = os.path.join(FIXTURE_DIR, "docusaurus.html")
MKDOCS_FIXTURE = os.path.join(FIXTURE_DIR, "mkdocs.html")

# The fixture is a snapshot of https://docs.trilio.io/kubernetes
ROOT = "https://docs.trilio.io/kubernetes"


def _read(path: str) -> str:
    return open(path, encoding="utf-8").read()


# ---------------------------------------------------------------------------
# Detection tests
# ---------------------------------------------------------------------------

def test_detect_matches_gitbook():
    assert GitBookProfile().detect(_read(GITBOOK_FIXTURE), ROOT) is True


def test_detect_rejects_commvault():
    assert GitBookProfile().detect(_read(COMMVAULT_FIXTURE), ROOT) is False


def test_detect_rejects_docusaurus():
    assert GitBookProfile().detect(_read(DOCUSAURUS_FIXTURE), ROOT) is False


def test_detect_rejects_mkdocs():
    assert GitBookProfile().detect(_read(MKDOCS_FIXTURE), ROOT) is False


# ---------------------------------------------------------------------------
# Content config
# ---------------------------------------------------------------------------

def test_content_config_has_only_main_content():
    cfg = GitBookProfile().content_config()
    assert cfg["onlyMainContent"] is True


def test_content_config_has_wait_for():
    cfg = GitBookProfile().content_config()
    assert cfg["waitFor"] == 3000


# ---------------------------------------------------------------------------
# TOC building
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_build_toc_is_non_empty():
    toc = await GitBookProfile().build_toc(ROOT, FakeScraper({ROOT: _read(GITBOOK_FIXTURE)}))
    assert len(toc) > 0


@pytest.mark.asyncio
async def test_build_toc_entries_have_title_and_url():
    toc = await GitBookProfile().build_toc(ROOT, FakeScraper({ROOT: _read(GITBOOK_FIXTURE)}))
    # All article entries must have a non-empty title and URL
    for entry in toc:
        assert entry.title, f"Empty title for entry: {entry}"
        if entry.is_article:
            assert entry.url, f"Article entry has empty url: {entry}"


@pytest.mark.asyncio
async def test_build_toc_contains_known_titles():
    """Spot-check titles that are present in the Trilio GitBook fixture."""
    toc = await GitBookProfile().build_toc(ROOT, FakeScraper({ROOT: _read(GITBOOK_FIXTURE)}))
    titles = [e.title for e in toc]
    # Top-level section headers (button-label sections, no URL)
    assert "Overview" in titles
    assert "Installation" in titles
    # Article entries nested under sections
    assert "Welcome" in titles
    assert "Features" in titles
    assert "Use Cases" in titles


@pytest.mark.asyncio
async def test_build_toc_has_nesting():
    """GitBook fixture has section wrappers; articles must appear at level ≥ 1."""
    toc = await GitBookProfile().build_toc(ROOT, FakeScraper({ROOT: _read(GITBOOK_FIXTURE)}))
    nested = [e for e in toc if e.level >= 1]
    assert len(nested) >= 1, "Expected at least one nested (level>=1) entry"


@pytest.mark.asyncio
async def test_build_toc_dom_order_preserved():
    """Welcome (first article) must appear before Features (first child article)."""
    toc = await GitBookProfile().build_toc(ROOT, FakeScraper({ROOT: _read(GITBOOK_FIXTURE)}))
    titles = [e.title for e in toc]
    assert titles.index("Welcome") < titles.index("Features")


@pytest.mark.asyncio
async def test_build_toc_no_duplicates():
    """Each (title, url) pair must appear exactly once (no duplication from the
    button-section / nested-ul structure)."""
    toc = await GitBookProfile().build_toc(ROOT, FakeScraper({ROOT: _read(GITBOOK_FIXTURE)}))
    # Use (title, url) pairs for uniqueness; section entries share url=""
    pairs = [(e.title, e.url) for e in toc]
    assert len(pairs) == len(set(pairs)), "Duplicate (title, url) entries detected in TOC"
