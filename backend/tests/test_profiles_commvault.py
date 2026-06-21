import sys
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.services.profiles.commvault import CommvaultProfile
from app.services.profiles.scraper import FakeScraper

BASE = "https://documentation.commvault.com/11.44/software/"
INDEX_ROOT = BASE + "index.html"
SECTION_ROOT = BASE + "get_started_with_commvault.html"

GS = "nav__get_started_with_commvault"

# Full-mode fixture: a top-level listing (__TOP__) plus each section's depth-first
# expansion (section node at level 0 + descendants). "Protect" is a url-less
# category with a child.
FULL_TOC = {
    "__TOP__": [
        {"id": GS, "href": "get_started_with_commvault.html", "title": "Get started", "level": 0, "isParent": True},
        {"id": "nav__protect", "href": None, "title": "Protect", "level": 0, "isParent": True},
    ],
    GS: [
        {"id": GS, "href": "get_started_with_commvault.html", "title": "Get started", "level": 0, "isParent": True},
        {"id": "x1", "href": "deploy_infra.html", "title": "Deploy infrastructure", "level": 1, "isParent": False},
        {"id": "x2", "href": "configure_network.html", "title": "Configure network", "level": 1, "isParent": False},
    ],
    "nav__protect": [
        {"id": "nav__protect", "href": None, "title": "Protect", "level": 0, "isParent": True},
        {"id": "x3", "href": "cloud_discovery.html", "title": "Cloud discovery", "level": 1, "isParent": False},
    ],
}


# ── Detection ────────────────────────────────────────────────────────────────

def test_detect_matches_commvault_host():
    assert CommvaultProfile().detect("<html>Loading…</html>", INDEX_ROOT) is True


def test_detect_matches_old_inline_nav():
    html = '<div id="nav"><ul class="nav-group"></ul></div>'
    assert CommvaultProfile().detect(html, "https://docs.example.com/x.html") is True


def test_detect_rejects_other_platforms():
    assert CommvaultProfile().detect(
        "<html><body><main>hi</main></body></html>", "https://example.com/"
    ) is False


# ── Content config ───────────────────────────────────────────────────────────

def test_content_config_scopes_to_doc_and_drops_breadcrumb():
    cfg = CommvaultProfile().content_config()
    assert cfg["includeTags"] == ["#doc"]
    assert cfg["excludeTags"] == [".breadcrumbs"]


# ── FULL mode (index): top-level listing + per-section expansion, combined ───

@pytest.mark.asyncio
async def test_full_mode_combines_sections_in_order():
    scraper = FakeScraper({}, toc_by_url=FULL_TOC)
    toc = await CommvaultProfile().build_toc(INDEX_ROOT, scraper)
    got = [(e.title, e.level, e.is_article, e.url) for e in toc]
    assert got == [
        ("Get started", 0, True, BASE + "get_started_with_commvault.html"),
        ("Deploy infrastructure", 1, True, BASE + "deploy_infra.html"),
        ("Configure network", 1, True, BASE + "configure_network.html"),
        ("Protect", 0, False, None),                       # url-less category section
        ("Cloud discovery", 1, True, BASE + "cloud_discovery.html"),
    ]


@pytest.mark.asyncio
async def test_full_mode_parent_links():
    scraper = FakeScraper({}, toc_by_url=FULL_TOC)
    toc = await CommvaultProfile().build_toc(INDEX_ROOT, scraper)
    by = {e.title: e for e in toc}
    assert by["Get started"].parent_url is None
    assert by["Deploy infrastructure"].parent_url == by["Get started"].url
    # Child of a url-less category → no parent_url (downstream level-adjacency links it).
    assert by["Cloud discovery"].parent_url is None


# ── SECTION mode (specific page): scoped to nav__<key> ───────────────────────

@pytest.mark.asyncio
async def test_section_mode_scopes_to_page_subtree():
    scraper = FakeScraper({}, toc_by_url=FULL_TOC)
    toc = await CommvaultProfile().build_toc(SECTION_ROOT, scraper)
    assert [e.title for e in toc] == ["Get started", "Deploy infrastructure", "Configure network"]


@pytest.mark.asyncio
async def test_section_id_derivation():
    captured = []

    class CapturingScraper(FakeScraper):
        async def expand_toc(self, url, section_id=None):
            captured.append(section_id)
            return await super().expand_toc(url, section_id)

    sc = CapturingScraper({}, toc_by_url=FULL_TOC)
    await CommvaultProfile().build_toc(SECTION_ROOT, sc)
    assert captured == [GS]


@pytest.mark.asyncio
async def test_index_root_lists_top_level_first():
    captured = []

    class CapturingScraper(FakeScraper):
        async def expand_toc(self, url, section_id=None):
            captured.append(section_id)
            return await super().expand_toc(url, section_id)

    sc = CapturingScraper({}, toc_by_url=FULL_TOC)
    await CommvaultProfile().build_toc(INDEX_ROOT, sc)
    assert captured[0] == "__TOP__"
    assert GS in captured and "nav__protect" in captured
