import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.services.profiles.cohesity import CohesityProfile, parse_cohesity_sidebar
from app.services.profiles.scraper import FakeScraper

ROOT = "https://docs.cohesity.com/docs/netbackup/11.2/103228346-171368441-0/v95650213-171368441"


# A fully-expanded Cohesity sidebar (what Browserless returns after clicking every
# collapsed trigger): a plain leaf, a pure section (button-only) with a nested
# sub-section, and a section that is ALSO a link (anchor + children) — mirroring
# the real shadcn/ui + radix Collapsible structure where a <div data-slot=
# "collapsible"> wraps the label <li> and a sibling <div data-slot=
# "collapsible-content"> holds the child <ul data-slot="sidebar-menu">.
EXPANDED_SIDEBAR = """
<div data-slot="sidebar-inner">
  <div data-slot="sidebar-content">
    <div data-slot="sidebar-group"><div data-slot="sidebar-group-content">
      <ul data-slot="sidebar-menu">
        <li data-slot="sidebar-menu-item"><a href="/q">Quick Start</a></li>
        <div data-slot="collapsible" data-state="open">
          <li data-slot="sidebar-menu-item"><button data-slot="collapsible-trigger">Admin Guide</button></li>
          <div data-slot="collapsible-content">
            <ul data-slot="sidebar-menu">
              <li data-slot="sidebar-menu-item"><a href="/a/c1">Chapter 1</a></li>
              <div data-slot="collapsible" data-state="open">
                <li data-slot="sidebar-menu-item"><button data-slot="collapsible-trigger">Advanced</button></li>
                <div data-slot="collapsible-content">
                  <ul data-slot="sidebar-menu">
                    <li data-slot="sidebar-menu-item"><a href="/a/adv/tuning">Tuning</a></li>
                  </ul>
                </div>
              </div>
            </ul>
          </div>
        </div>
        <div data-slot="collapsible" data-state="open">
          <li data-slot="sidebar-menu-item"><a href="/rn">Release Notes</a><button data-slot="collapsible-trigger"></button></li>
          <div data-slot="collapsible-content">
            <ul data-slot="sidebar-menu">
              <li data-slot="sidebar-menu-item"><a href="/rn/about">About</a></li>
            </ul>
          </div>
        </div>
      </ul>
    </div></div>
  </div>
</div>
"""

# A single (collapsed) render: radix has not mounted any collapsible-content
# children yet, so only the top-level labels are present.
COLLAPSED_ROOT = """
<html><body><main><div data-slot="sidebar-inner">
  <div data-slot="sidebar-content"><div data-slot="sidebar-group"><div data-slot="sidebar-group-content">
    <ul data-slot="sidebar-menu">
      <li data-slot="sidebar-menu-item"><a href="/q">Quick Start</a></li>
      <div data-slot="collapsible" data-state="closed">
        <li data-slot="sidebar-menu-item"><button data-slot="collapsible-trigger">Admin Guide</button></li>
        <div data-slot="collapsible-content"></div>
      </div>
      <div data-slot="collapsible" data-state="closed">
        <li data-slot="sidebar-menu-item"><button data-slot="collapsible-trigger">Release Notes</button></li>
        <div data-slot="collapsible-content"></div>
      </div>
    </ul>
  </div></div></div>
</div></main></body></html>
"""


def test_detect_matches_cohesity_host():
    assert CohesityProfile().detect("<html></html>", ROOT) is True


def test_detect_matches_by_markers_on_other_host():
    html = '<div data-slot="sidebar-inner"><button data-slot="collapsible-trigger"></button></div>'
    assert CohesityProfile().detect(html, "https://example.com/docs") is True


def test_detect_rejects_unrelated_site():
    assert CohesityProfile().detect("<div class='theme-doc-sidebar-menu'></div>",
                                    "https://docs.portworx.com/") is False


def test_content_config():
    cfg = CohesityProfile().content_config()
    assert cfg["includeTags"] == ["article"]
    assert cfg["onlyMainContent"] is False


def test_parse_expanded_nested_tree():
    toc = parse_cohesity_sidebar(EXPANDED_SIDEBAR, ROOT)
    by_title = {e.title: e for e in toc}
    assert set(by_title) == {"Quick Start", "Admin Guide", "Chapter 1",
                             "Advanced", "Tuning", "Release Notes", "About"}
    # Levels reflect nesting depth.
    assert by_title["Quick Start"].level == 0
    assert by_title["Admin Guide"].level == 0
    assert by_title["Chapter 1"].level == 1
    assert by_title["Advanced"].level == 1
    assert by_title["Tuning"].level == 2
    assert by_title["About"].level == 1
    # Pure sections (trigger, no link) are url-less; leaves are articles.
    assert by_title["Admin Guide"].url is None
    assert by_title["Admin Guide"].is_article is False
    assert by_title["Tuning"].is_article is True
    assert by_title["Tuning"].url == "https://docs.cohesity.com/a/adv/tuning"
    # A section that is ALSO a link keeps its url but is not a leaf article.
    assert by_title["Release Notes"].url == "https://docs.cohesity.com/rn"
    assert by_title["Release Notes"].is_article is False
    # Children nest under the right parent: a linked section passes its url down.
    assert by_title["About"].parent_url == "https://docs.cohesity.com/rn"
    # Url-less sections (Admin Guide, Advanced) carry no url to descend with, so
    # their children link by level adjacency downstream (parent_url stays None).
    assert by_title["Tuning"].parent_url is None


@pytest.mark.asyncio
async def test_build_toc_expands_full_tree():
    scraper = FakeScraper({ROOT: COLLAPSED_ROOT},
                          collapsible_sidebar_by_url={ROOT: EXPANDED_SIDEBAR})
    toc = await CohesityProfile().build_toc(ROOT, scraper)
    titles = {e.title for e in toc}
    assert "Tuning" in titles and "About" in titles and "Chapter 1" in titles
    assert len(toc) == 7


@pytest.mark.asyncio
async def test_build_toc_falls_back_to_single_render_without_browserless():
    """No Browserless fixture → BrowserlessError → parse the single render
    (top-level labels only, since collapsed children aren't mounted)."""
    toc = await CohesityProfile().build_toc(ROOT, FakeScraper({ROOT: COLLAPSED_ROOT}))
    titles = [e.title for e in toc]
    assert titles == ["Quick Start", "Admin Guide", "Release Notes"]
    # Quick Start is a real leaf link; the two collapsed guides are url-less.
    by_title = {e.title: e for e in toc}
    assert by_title["Quick Start"].is_article is True
    assert by_title["Admin Guide"].url is None
