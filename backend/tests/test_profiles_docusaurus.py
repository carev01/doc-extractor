import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.services.profiles.docusaurus import DocusaurusProfile
from app.services.profiles.scraper import FakeScraper

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "platforms", "docusaurus.html")
COMMVAULT_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "platforms", "commvault.html")
ROOT = "https://docs.portworx.com/portworx-backup-on-prem/"


def _html():
    return open(FIXTURE, encoding="utf-8").read()


def _commvault_html():
    return open(COMMVAULT_FIXTURE, encoding="utf-8").read()


def test_detect_matches_docusaurus():
    assert DocusaurusProfile().detect(_html(), ROOT) is True


def test_detect_rejects_commvault():
    assert DocusaurusProfile().detect(_commvault_html(), ROOT) is False


def test_content_config():
    cfg = DocusaurusProfile().content_config()
    assert cfg["includeTags"] == [".theme-doc-markdown"]
    assert cfg["onlyMainContent"] is False
    assert cfg["waitFor"] == 1500


@pytest.mark.asyncio
async def test_build_toc_yields_ordered_entries():
    toc = await DocusaurusProfile().build_toc(ROOT, FakeScraper({ROOT: _html()}))
    assert len(toc) > 0
    # All entries must have non-empty titles and urls
    assert all(e.title and e.url for e in toc)
    # Levels must be non-negative (fixture has all level 0 since sidebar is collapsed)
    assert all(e.level >= 0 for e in toc)
    # Verify specific top-level titles parsed from the fixture
    titles = [e.title for e in toc]
    assert "Portworx Backup Documentation" in titles
    assert "What's New in Portworx Backup" in titles
    assert "Release Notes" in titles
    # All 11 sidebar items should be present
    assert len(toc) == 11
