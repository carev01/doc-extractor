import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.services.profiles.commvault import CommvaultProfile
from app.services.profiles.scraper import FakeScraper

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "platforms", "commvault.html")
ROOT = "https://documentation.commvault.com/clumio/index.html"


def _html():
    return open(FIXTURE, encoding="utf-8").read()


def test_detect_matches_commvault():
    assert CommvaultProfile().detect(_html(), ROOT) is True


def test_detect_rejects_other_markup():
    assert CommvaultProfile().detect("<html><body><main>hi</main></body></html>", ROOT) is False


def test_content_config_scopes_to_doc():
    assert CommvaultProfile().content_config()["includeTags"] == ["#doc"]


@pytest.mark.asyncio
async def test_build_toc_yields_ordered_top_level():
    # FakeScraper serves the root nav; child (parent) URLs aren't served, so
    # recursion stops there and we get the ordered level-0 entries.
    toc = await CommvaultProfile().build_toc(ROOT, FakeScraper({ROOT: _html()}))
    assert len(toc) > 0
    assert all(e.level == 0 for e in toc)            # only root level resolved here
    assert all(e.title and e.url for e in toc)        # real titles + links
