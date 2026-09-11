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
  - content_config() targets [data-mc-content-body] and #mc-main-content.
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
LAZY_TREE_FIXTURE = os.path.join(FIXTURE_DIR, "lazy_tree.html")
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


def test_detect_rejects_lazy_tree():
    assert FlareWebHelpProfile().detect(_read(LAZY_TREE_FIXTURE), ROOT) is False


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
    # A single [role=main] selector — MadCap's content body carries role="main"
    # in both the WebHelp/TriPane and HTML5 skins. Firecrawl ignores id selectors
    # like #mc-main-content, and on some MadCap pages (table/iframe-wrapped, e.g.
    # Acronis) any multi-selector includeTags list collapses to empty/fragments —
    # so a single attribute selector is the reliable scope.
    assert cfg["includeTags"] == ["[role=main]"]


def test_content_config_excludes_skin_chrome():
    cfg = FlareWebHelpProfile().content_config()
    assert cfg["excludeTags"] == [".GoToTop", ".feedback-button", ".nocontent"]


def test_content_config_only_main_content_false():
    cfg = FlareWebHelpProfile().content_config()
    assert cfg["onlyMainContent"] is False


def test_content_config_wait_for():
    cfg = FlareWebHelpProfile().content_config()
    assert cfg["waitFor"] == 1500


# ---------------------------------------------------------------------------
# Raw-HTTP content engine (static server-rendered topics)
# ---------------------------------------------------------------------------

# Minimal but representative static topic: the body in #mc-main-content, plus
# the page chrome (header nav, mini-TOC) that the live JS would otherwise turn
# into a dynamic shell. A raw (un-rendered) GET returns all of this.
STATIC_TOPIC = """
<html><body>
  <header id="nav"><a href="#">Logout</a><span>Español</span></header>
  <div role="main" id="mc-main-content">
    <h1>Unattended installation in Windows</h1>
    <p>Install via the <code>msiexec</code> program.</p>
    <div class="MCMiniTocBox_0 miniToc nocontent">Mini TOC noise</div>
    <a class="GoToTop" href="#top">Back to top</a>
    <img src="Resources/Images/diagram.png" alt="diagram"/>
    <img src="/shared/logo.png" alt="logo"/>
  </div>
  <footer>PreviousNext</footer>
</body></html>
"""

TOPIC_URL = HELP_ROOT + "unattended-installation-in-windows.htm"


def test_content_engine_is_raw_http():
    # Topics are static; the site's own JS replaces the body with a dynamic
    # shell, so content must be scraped without rendering.
    assert FlareWebHelpProfile().content_engine == "raw_http"


def test_extract_content_html_scopes_to_body():
    html = FlareWebHelpProfile().extract_content_html(STATIC_TOPIC, TOPIC_URL)
    assert html is not None
    assert "Unattended installation in Windows" in html
    assert "msiexec" in html
    # Page chrome outside the body container is excluded.
    assert "Logout" not in html
    assert "Español" not in html
    assert "PreviousNext" not in html


def test_extract_content_html_drops_skin_chrome():
    """Skin chrome inside the body container (mini-TOC, back-to-top) is removed
    via the profile's excludeTags."""
    html = FlareWebHelpProfile().extract_content_html(STATIC_TOPIC, TOPIC_URL)
    assert "Mini TOC noise" not in html  # .nocontent
    assert "Back to top" not in html     # .GoToTop


def test_extract_content_html_resolves_relative_images():
    """Relative image srcs are absolutised against the topic URL so the
    downstream image download/rewrite step can match them."""
    html = FlareWebHelpProfile().extract_content_html(STATIC_TOPIC, TOPIC_URL)
    assert HELP_ROOT + "Resources/Images/diagram.png" in html
    # Root-relative src resolves against the host, not the help root.
    assert "https://documentation.arcserve.com/shared/logo.png" in html


def test_extract_content_html_scopes_html5_attr_body():
    """A topic that scopes its body with the HTML5 [data-mc-content-body]
    attribute (rather than #mc-main-content) is extracted too. The two
    selectors don't co-occur on a real topic (see content_config), so this
    mirrors a genuine html5-skin topic carrying only the attribute."""
    html_doc = (
        '<html><body><header>Logout</header>'
        '<div data-mc-content-body="True"><h1>Topic</h1><p>real body</p></div>'
        '</body></html>'
    )
    out = FlareWebHelpProfile().extract_content_html(html_doc, TOPIC_URL)
    assert out is not None
    assert "real body" in out
    assert "Logout" not in out


def test_extract_content_html_returns_none_when_no_body():
    """A page with no body container (e.g. a redirect/placeholder) yields None
    so the caller skips it rather than storing chrome."""
    html_doc = "<html><body><header>nav only</header></body></html>"
    assert FlareWebHelpProfile().extract_content_html(html_doc, TOPIC_URL) is None


# ── Topics published without a skin content container ────────────────────────
# Arcserve Console On-Premises serves 420 of its 734 topics this way: real
# content sitting bare in <body>, with neither data-mc-content-body nor
# id="mc-main-content". Returning None for all of them failed 57% of the pages
# and tripped the raw_http failure-rate guard, failing the whole run.

def _unwrapped_topic(body_inner: str, kind: str = "Topic") -> str:
    """A Flare topic whose body carries no skin content container."""
    return (
        f'<html data-mc-target-type="WebHelp2" data-mc-runtime-file-type="{kind}">'
        f"<head><title>t</title></head><body>{body_inner}</body></html>"
    )


def test_extract_content_html_recovers_unwrapped_topic():
    out = FlareWebHelpProfile().extract_content_html(
        _unwrapped_topic(
            '<h1 class="heading1">Communication Ports</h1>'
            '<p class="bodytext">This section provides information.</p>'
        ),
        TOPIC_URL,
    )
    assert out is not None
    assert "Communication Ports" in out
    assert "This section provides information." in out


def test_unwrapped_fallback_still_skips_the_help_system_shell():
    """The guarantee the None return exists for. default.htm has no container
    either, but Flare labels it Default;TriPane and it holds only navigation —
    storing it would mean a chrome-only article per help system."""
    shell = _unwrapped_topic(
        '<h1>Log Console</h1><a href="#">Skip To Main Content</a>'
        '<a href="#">Account Settings</a><a href="#">Logout</a>',
        kind="Default;TriPane",
    )
    assert FlareWebHelpProfile().extract_content_html(shell, TOPIC_URL) is None


def test_unwrapped_fallback_requires_the_flare_topic_marker():
    """No marker at all (an arbitrary HTML page) is still skipped, so the
    fallback can't turn every unrecognised page into an article."""
    html_doc = (
        "<html><head><title>t</title></head>"
        '<body><h1>Something</h1><p>Body text.</p></body></html>'
    )
    assert FlareWebHelpProfile().extract_content_html(html_doc, TOPIC_URL) is None


def test_unwrapped_fallback_reads_topic_as_one_token_of_a_list():
    """data-mc-runtime-file-type is a ';'-separated list, so match on the token
    rather than the whole string."""
    out = FlareWebHelpProfile().extract_content_html(
        _unwrapped_topic('<h1>T</h1><p>Real body.</p>', kind="Topic;Something"),
        TOPIC_URL,
    )
    assert out is not None and "Real body." in out


def test_unwrapped_fallback_drops_chrome_and_scripts():
    """The fallback keeps whatever <body> held, so page furniture the wrapped
    skin puts *outside* its content container must be dropped here."""
    out = FlareWebHelpProfile().extract_content_html(
        _unwrapped_topic(
            '<div class="back-to-bookshelf-container">Bookshelf</div>'
            '<ul class="breadcrumb"><li>You are here</li></ul>'
            '<script>var tracking = 1;</script>'
            '<style>.x { color: red }</style>'
            '<h1>Real Title</h1><p class="bodytext">Real body.</p>'
            '<div class="GoToTop">Top</div>'
        ),
        TOPIC_URL,
    )
    assert out is not None
    assert "Real Title" in out and "Real body." in out
    for gone in ("Bookshelf", "You are here", "tracking", "color: red", "Top"):
        assert gone not in out, gone


def test_wrapped_topic_is_unaffected_by_the_fallback():
    """A topic that *does* have a container must scope to the container, not to
    <body> — otherwise the fallback would silently widen every existing page."""
    html_doc = _unwrapped_topic(
        '<div class="back-to-bookshelf-container">chrome</div>'
        '<div id="mc-main-content"><h1>Scoped</h1><p>Inside.</p></div>'
    )
    out = FlareWebHelpProfile().extract_content_html(html_doc, TOPIC_URL)
    assert out is not None
    assert "Inside." in out
    assert "chrome" not in out
    assert out.strip().startswith('<div id="mc-main-content"')


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
