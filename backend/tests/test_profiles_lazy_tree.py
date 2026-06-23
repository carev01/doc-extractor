import sys
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.services.profiles.lazy_tree import LazyTreeProfile
from app.services.profiles.scraper import FakeScraper

BASE = "https://documentation.commvault.com/11.44/software/"
INDEX_ROOT = BASE + "index.html"
SECTION_ROOT = BASE + "get_started_with_commvault.html"

GS = "nav__get_started_with_commvault"

# A single Browserless session expands the whole tree depth-first and returns one
# ordered node list. The full-mode fixture (keyed by the index URL, i.e.
# section_id=None) is that flat depth-first walk; the section-mode fixture (keyed
# by the section's <li id>) is just that section's subtree. "Protect" is a
# url-less category with a child.
FULL_TOC = {
    INDEX_ROOT: [
        {"id": GS, "href": "get_started_with_commvault.html", "title": "Get started", "level": 0, "isParent": True},
        {"id": "x1", "href": "deploy_infra.html", "title": "Deploy infrastructure", "level": 1, "isParent": False},
        {"id": "x2", "href": "configure_network.html", "title": "Configure network", "level": 1, "isParent": False},
        {"id": "nav__protect", "href": None, "title": "Protect", "level": 0, "isParent": True},
        {"id": "x3", "href": "cloud_discovery.html", "title": "Cloud discovery", "level": 1, "isParent": False},
    ],
    GS: [
        {"id": GS, "href": "get_started_with_commvault.html", "title": "Get started", "level": 0, "isParent": True},
        {"id": "x1", "href": "deploy_infra.html", "title": "Deploy infrastructure", "level": 1, "isParent": False},
        {"id": "x2", "href": "configure_network.html", "title": "Configure network", "level": 1, "isParent": False},
    ],
}

# Checkpoint (resumable) fixture: a top-level listing keyed by "__TOP__" plus
# each section's subtree keyed by its <li id>. build_toc expands one section per
# call and persists it, so the index can resume mid-build.
CKPT_TOC = {
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


class FakeCheckpoint:
    """In-memory stand-in for TocBuildCheckpoint."""

    def __init__(self, initial: dict | None = None):
        self.data = dict(initial or {})
        self.cleared = False

    async def load(self) -> dict:
        return dict(self.data)

    async def save_top_level(self, tops):
        self.data["top_level"] = tops

    async def save_section(self, section_id, nodes):
        self.data.setdefault("sections", {})[section_id] = nodes

    async def clear(self):
        self.cleared = True
        self.data = {}


# ── Detection ────────────────────────────────────────────────────────────────

def test_detect_matches_lazy_tree_host():
    assert LazyTreeProfile().detect("<html>Loading…</html>", INDEX_ROOT) is True


def test_detect_matches_old_inline_nav():
    html = '<div id="nav"><ul class="nav-group"></ul></div>'
    assert LazyTreeProfile().detect(html, "https://docs.example.com/x.html") is True


def test_detect_rejects_other_platforms():
    assert LazyTreeProfile().detect(
        "<html><body><main>hi</main></body></html>", "https://example.com/"
    ) is False


# ── Content config ───────────────────────────────────────────────────────────

def test_content_config_scopes_to_doc_and_drops_breadcrumb():
    cfg = LazyTreeProfile().content_config()
    assert cfg["includeTags"] == ["#doc"]
    assert cfg["excludeTags"] == [".breadcrumbs"]


# ── FULL mode (index): top-level listing + per-section expansion, combined ───

@pytest.mark.asyncio
async def test_full_mode_combines_sections_in_order():
    scraper = FakeScraper({}, toc_by_url=FULL_TOC)
    toc = await LazyTreeProfile().build_toc(INDEX_ROOT, scraper)
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
    toc = await LazyTreeProfile().build_toc(INDEX_ROOT, scraper)
    by = {e.title: e for e in toc}
    assert by["Get started"].parent_url is None
    assert by["Deploy infrastructure"].parent_url == by["Get started"].url
    # Child of a url-less category → no parent_url (downstream level-adjacency links it).
    assert by["Cloud discovery"].parent_url is None


# ── SECTION mode (specific page): scoped to nav__<key> ───────────────────────

@pytest.mark.asyncio
async def test_section_mode_scopes_to_page_subtree():
    scraper = FakeScraper({}, toc_by_url=FULL_TOC)
    toc = await LazyTreeProfile().build_toc(SECTION_ROOT, scraper)
    assert [e.title for e in toc] == ["Get started", "Deploy infrastructure", "Configure network"]


@pytest.mark.asyncio
async def test_section_id_derivation():
    captured = []

    class CapturingScraper(FakeScraper):
        async def expand_toc(self, url, section_id=None):
            captured.append(section_id)
            return await super().expand_toc(url, section_id)

    sc = CapturingScraper({}, toc_by_url=FULL_TOC)
    await LazyTreeProfile().build_toc(SECTION_ROOT, sc)
    assert captured == [GS]


@pytest.mark.asyncio
async def test_index_root_no_checkpoint_expands_whole_tree_in_one_call():
    """index.html without a checkpoint (e.g. tests) → a single expand_toc call
    with section_id=None (one session walks the whole tree)."""
    captured = []

    class CapturingScraper(FakeScraper):
        async def expand_toc(self, url, section_id=None):
            captured.append(section_id)
            return await super().expand_toc(url, section_id)

    sc = CapturingScraper({}, toc_by_url=FULL_TOC)  # checkpoint defaults to None
    await LazyTreeProfile().build_toc(INDEX_ROOT, sc)
    assert captured == [None]


# ── FULL mode (index) with a checkpoint: resumable per-section expansion ─────

class _CapturingScraper(FakeScraper):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.expanded = []

    async def expand_toc(self, url, section_id=None):
        self.expanded.append(section_id)
        return await super().expand_toc(url, section_id)


@pytest.mark.asyncio
async def test_checkpointed_index_lists_top_then_expands_each_section():
    ckpt = FakeCheckpoint()
    sc = _CapturingScraper({}, toc_by_url=CKPT_TOC, checkpoint=ckpt)
    toc = await LazyTreeProfile().build_toc(INDEX_ROOT, sc)
    # __TOP__ first, then one call per top-level section, in order.
    assert sc.expanded == ["__TOP__", GS, "nav__protect"]
    assert [(e.title, e.level) for e in toc] == [
        ("Get started", 0), ("Deploy infrastructure", 1), ("Configure network", 1),
        ("Protect", 0), ("Cloud discovery", 1),
    ]
    # build_toc does NOT clear — the checkpoint carries into the content phase
    # and is cleared by the caller (extract_source) once the whole run completes.
    assert ckpt.cleared is False


@pytest.mark.asyncio
async def test_checkpointed_index_resumes_and_skips_done_sections():
    # Resume state: top-level already read, "Get started" already expanded.
    ckpt = FakeCheckpoint({
        "top_level": CKPT_TOC["__TOP__"],
        "sections": {GS: CKPT_TOC[GS]},
    })
    sc = _CapturingScraper({}, toc_by_url=CKPT_TOC, checkpoint=ckpt)
    toc = await LazyTreeProfile().build_toc(INDEX_ROOT, sc)
    # Neither __TOP__ nor the done section is re-expanded — only "Protect".
    assert sc.expanded == ["nav__protect"]
    titles = [e.title for e in toc]
    assert titles == ["Get started", "Deploy infrastructure", "Configure network",
                      "Protect", "Cloud discovery"]
    assert ckpt.cleared is False  # cleared later by extract_source, not build_toc


@pytest.mark.asyncio
async def test_checkpointed_index_persists_each_section_as_it_completes():
    ckpt = FakeCheckpoint()

    saved = []
    orig_save = ckpt.save_section

    async def tracking_save(sid, nodes):
        saved.append(sid)
        await orig_save(sid, nodes)

    ckpt.save_section = tracking_save
    sc = _CapturingScraper({}, toc_by_url=CKPT_TOC, checkpoint=ckpt)
    await LazyTreeProfile().build_toc(INDEX_ROOT, sc)
    # Each expanded section was persisted before the build finished.
    assert saved == [GS, "nav__protect"]
