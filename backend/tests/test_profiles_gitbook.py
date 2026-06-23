"""Tests for the GitBook extraction profile."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.profiles.gitbook import GitBookProfile
from app.services.profiles.scraper import FakeScraper
from app.services.browserless import BrowserlessError

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "platforms")
GITBOOK_FIXTURE = os.path.join(FIXTURE_DIR, "gitbook.html")
LAZY_TREE_FIXTURE = os.path.join(FIXTURE_DIR, "lazy_tree.html")
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


def test_detect_rejects_lazy_tree():
    assert GitBookProfile().detect(_read(LAZY_TREE_FIXTURE), ROOT) is False


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


# ---------------------------------------------------------------------------
# Recursive per-page crawl (full tree). GitBook's sidebar is contextual: a
# node's children appear only when you navigate to it, so build_toc visits
# every page (Browserless) and merges each one's revealed direct children.
# ---------------------------------------------------------------------------

GB_ROOT = "https://ex.com/docs"

# Root page: Welcome (leaf) + url-less "Guide" section with Intro + Setup.
GB_ROOT_ASIDE = """
<aside data-testid="table-of-contents"><ul>
  <li><a href="/docs">Welcome</a></li>
  <li>
    <div><button>Guide</button></div>
    <div><ul>
      <li><a href="/docs/guide/intro">Intro</a></li>
      <li><a href="/docs/guide/setup">Setup</a></li>
    </ul></div>
  </li>
</ul></aside>
"""

# Intro page: same tree, but "Intro" is expanded showing its two children.
GB_INTRO_ASIDE = """
<aside data-testid="table-of-contents"><ul>
  <li><a href="/docs">Welcome</a></li>
  <li>
    <div><button>Guide</button></div>
    <div><ul>
      <li>
        <a href="/docs/guide/intro">Intro</a>
        <ul>
          <li><a href="/docs/guide/intro/a">Intro A</a></li>
          <li><a href="/docs/guide/intro/b">Intro B</a></li>
        </ul>
      </li>
      <li><a href="/docs/guide/setup">Setup</a></li>
    </ul></div>
  </li>
</ul></aside>
"""

GB_SIDEBARS = {
    GB_ROOT: GB_ROOT_ASIDE,
    "https://ex.com/docs/guide/intro": GB_INTRO_ASIDE,
    "https://ex.com/docs/guide/setup": GB_ROOT_ASIDE,   # Setup has no child <ul>
    "https://ex.com/docs/guide/intro/a": GB_INTRO_ASIDE,  # leaf (A has no child <ul>)
    "https://ex.com/docs/guide/intro/b": GB_INTRO_ASIDE,  # leaf
}


class FakeCheckpoint:
    def __init__(self, initial=None):
        self.data = dict(initial or {})
        self.cleared = False

    async def load(self):
        return dict(self.data)

    async def save_data(self, patch):
        self.data.update(patch)

    async def clear(self):
        self.cleared = True
        self.data = {}


@pytest.mark.asyncio
async def test_crawl_assembles_full_nested_tree_in_order():
    sc = FakeScraper({}, gitbook_sidebars_by_url=GB_SIDEBARS)
    toc = await GitBookProfile().build_toc(GB_ROOT, sc)
    got = [(e.title, e.level, e.is_article, e.url) for e in toc]
    assert got == [
        ("Welcome", 0, True, GB_ROOT),
        ("Guide", 0, False, None),
        ("Intro", 1, True, "https://ex.com/docs/guide/intro"),
        ("Intro A", 2, True, "https://ex.com/docs/guide/intro/a"),
        ("Intro B", 2, True, "https://ex.com/docs/guide/intro/b"),
        ("Setup", 1, True, "https://ex.com/docs/guide/setup"),
    ]


@pytest.mark.asyncio
async def test_crawl_captures_deep_children_a_single_render_would_miss():
    sc = FakeScraper({}, gitbook_sidebars_by_url=GB_SIDEBARS)
    toc = await GitBookProfile().build_toc(GB_ROOT, sc)
    titles = [e.title for e in toc]
    assert "Intro A" in titles and "Intro B" in titles


@pytest.mark.asyncio
async def test_crawl_is_checkpointed():
    ckpt = FakeCheckpoint()
    sc = FakeScraper({}, gitbook_sidebars_by_url=GB_SIDEBARS, checkpoint=ckpt)
    await GitBookProfile().build_toc(GB_ROOT, sc)
    assert ckpt.data.get("gb_base")
    assert "/docs/guide/intro" in ckpt.data.get("gb_children", {})
    assert "/docs/guide/intro" in set(ckpt.data.get("gb_visited", []))


@pytest.mark.asyncio
async def test_crawl_resume_skips_already_visited_pages():
    base = [
        {"title": "Welcome", "url": GB_ROOT, "level": 0, "is_article": True},
        {"title": "Guide", "url": None, "level": 0, "is_article": False},
        {"title": "Intro", "url": "https://ex.com/docs/guide/intro", "level": 1, "is_article": True},
        {"title": "Setup", "url": "https://ex.com/docs/guide/setup", "level": 1, "is_article": True},
    ]
    ckpt = FakeCheckpoint({
        "gb_base": base,
        "gb_children": {"/docs/guide/intro": [
            ["https://ex.com/docs/guide/intro/a", "Intro A"],
            ["https://ex.com/docs/guide/intro/b", "Intro B"],
        ]},
        "gb_visited": ["/docs", "/docs/guide/intro", "/docs/guide/setup",
                       "/docs/guide/intro/a", "/docs/guide/intro/b"],
    })

    fetched = []

    class TrackingScraper(FakeScraper):
        async def gitbook_sidebars(self, urls):
            fetched.extend(urls)
            return await super().gitbook_sidebars(urls)

    sc = TrackingScraper({}, gitbook_sidebars_by_url=GB_SIDEBARS, checkpoint=ckpt)
    toc = await GitBookProfile().build_toc(GB_ROOT, sc)
    assert fetched == []  # nothing left to visit
    assert [e.title for e in toc] == ["Welcome", "Guide", "Intro", "Intro A", "Intro B", "Setup"]


@pytest.mark.asyncio
async def test_falls_back_to_single_render_when_browserless_unavailable():
    class NoBrowserless(FakeScraper):
        async def gitbook_sidebars(self, urls):
            raise BrowserlessError("not configured")

    sc = NoBrowserless({GB_ROOT: GB_ROOT_ASIDE})
    toc = await GitBookProfile().build_toc(GB_ROOT, sc)
    titles = [e.title for e in toc]
    # Fallback: top-level + first sub-level only (no Intro A/B).
    assert titles == ["Welcome", "Guide", "Intro", "Setup"]
    assert "Intro A" not in titles
