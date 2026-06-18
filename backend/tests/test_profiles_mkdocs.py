import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.services.profiles.mkdocs import MkDocsProfile
from app.services.profiles.scraper import FakeScraper

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "platforms", "mkdocs.html")
COMMVAULT_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "platforms", "commvault.html")
DOCUSAURUS_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "platforms", "docusaurus.html")
ROOT = "https://satoricyber.com/docs/"


def _html():
    return open(FIXTURE, encoding="utf-8").read()


def _commvault_html():
    return open(COMMVAULT_FIXTURE, encoding="utf-8").read()


def _docusaurus_html():
    return open(DOCUSAURUS_FIXTURE, encoding="utf-8").read()


def test_detect_matches_mkdocs():
    assert MkDocsProfile().detect(_html(), ROOT) is True


def test_detect_rejects_commvault():
    assert MkDocsProfile().detect(_commvault_html(), ROOT) is False


def test_detect_rejects_docusaurus():
    assert MkDocsProfile().detect(_docusaurus_html(), ROOT) is False


def test_content_config():
    cfg = MkDocsProfile().content_config()
    assert cfg["includeTags"] == ["article.md-content__inner"]
    assert cfg["onlyMainContent"] is False
    assert cfg["waitFor"] == 1500


@pytest.mark.asyncio
async def test_build_toc_yields_ordered_entries():
    toc = await MkDocsProfile().build_toc(ROOT, FakeScraper({ROOT: _html()}))
    assert len(toc) > 0
    # All entries must have non-empty titles and URLs
    assert all(e.title and e.url for e in toc)
    assert all(e.level >= 0 for e in toc)

    # Verify known top-level titles from the Satori MkDocs fixture
    titles = [e.title for e in toc]
    assert "Getting Started" in titles
    assert "Videos" in titles
    assert "Release Notes" in titles

    # The fixture has expanded nested sections; assert at least one level-1 entry
    level1_entries = [e for e in toc if e.level == 1]
    assert len(level1_entries) > 0, "Expected nested (level-1) entries from expanded MkDocs sections"

    # Verify a known level-1 nested entry: "Introduction to Satori" section is expanded
    assert "Introduction to Satori" in titles

    # DOM order: Getting Started (level-0) before Videos (level-0)
    assert titles.index("Getting Started") < titles.index("Videos")
