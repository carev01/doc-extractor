"""Tests for the fern profile (Fern docs, e.g. docs.eon.io).

Fern server-renders the article body (.fern-prose) and all sidebar links into
static HTML, so the profile runs on the raw_http path (with the realm session
injected for authenticated sources). In the raw (no-JS) sidebar the page links
sit in a flat list alongside a tab switcher and collapsible-section <button>s,
so build_toc collects every in-guide anchor and derives nesting from URL path
depth. Hermetic: FakeScraper serves canned HTML.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from app.services.profiles.scraper import FakeScraper
from app.services.profiles.detector import detect_platform
from app.services.profiles.content_scope import scope_content_html
from app.services.profiles.fern import FernProfile

ROOT = "https://docs.eon.io/user-guide/what-is-eon"

# Mirrors Fern's raw sidebar: a tab switcher + a flat page-link list (not a
# nested <ul>/<li> tree), plus a different-tab (/api) link and a duplicate.
PAGE = """
<html><body>
  <aside class="fern-sidebar-desktop">
    <ul><li><a href="/user-guide/what-is-eon">User Guide</a></li>
        <li><a href="/api/overview">API Reference</a></li></ul>
    <button>AWS</button>
    <ul>
      <li><a href="/user-guide/explore-the-console">Explore the Console</a></li>
      <li><a href="/user-guide/aws/resources/ec2">EC2</a></li>
      <li><a href="/user-guide/faqs/billing">Billing</a></li>
      <li><a href="/user-guide/what-is-eon">What Is Eon (dup)</a></li>
      <li><a href="/api/auth">API Auth</a></li>
    </ul>
  </aside>
  <main class="fern-main">
    <article class="w-content-width">
      <div class="fern-prose">
        <h1>What Is Eon?</h1>
        <p>Eon is a cloud backup platform.</p>
      </div>
      <div class="toc-root">On this page</div>
    </article>
    <footer>Was this page helpful?</footer>
  </main>
</body></html>
"""


def _scraper():
    return FakeScraper({}, raw_by_url={ROOT: PAGE})


def test_opts_into_raw_http():
    assert FernProfile().content_engine == "raw_http"


def test_detect_needs_both_hooks():
    prof = FernProfile()
    assert prof.detect(PAGE, ROOT) is True
    assert prof.detect('<aside class="fern-sidebar-desktop"></aside>', ROOT) is False
    assert prof.detect('<div class="fern-prose"></div>', ROOT) is False
    assert prof.detect("<html><body><p>hi</p></body></html>", "https://x/") is False


def test_detects_via_registry():
    assert detect_platform(PAGE, ROOT) == "fern"


@pytest.mark.asyncio
async def test_builds_toc_levels_from_url_depth():
    toc = await FernProfile().build_toc(ROOT, _scraper())
    shape = [(e.level, e.title, e.is_article) for e in toc]
    # Only in-guide (/user-guide) links, de-duped, in DOM order; level = path depth.
    assert shape == [
        (0, "User Guide", True),                 # /user-guide/what-is-eon
        (0, "Explore the Console", True),        # /user-guide/explore-the-console
        (2, "EC2", True),                        # /user-guide/aws/resources/ec2
        (1, "Billing", True),                    # /user-guide/faqs/billing
    ]
    assert all(e.url and e.is_article for e in toc)


@pytest.mark.asyncio
async def test_excludes_other_tab_and_dedupes():
    toc = await FernProfile().build_toc(ROOT, _scraper())
    urls = [e.url for e in toc]
    assert not any("/api/" in u for u in urls)            # other tab excluded
    assert urls.count("https://docs.eon.io/user-guide/what-is-eon") == 1  # deduped


@pytest.mark.asyncio
async def test_missing_sidebar_returns_empty():
    s = FakeScraper({}, raw_by_url={ROOT: "<html><body><div class='fern-prose'>x</div></body></html>"})
    assert await FernProfile().build_toc(ROOT, s) == []


def test_content_scopes_prose_and_drops_chrome():
    cfg = FernProfile().content_config()
    out = scope_content_html(PAGE, ROOT, cfg["includeTags"], cfg["excludeTags"])
    assert "cloud backup platform" in out      # body kept
    assert "What Is Eon?" in out               # h1 kept
    assert "On this page" not in out           # right-rail TOC dropped
    assert "Was this page helpful" not in out  # footer dropped
    assert "Explore the Console" not in out     # sidebar outside scope


# ── The embedded nav tree (current Fern builds) ──────────────────────────────
# Fern ships the whole sidebar as data in the Next.js flight payload. The raw DOM
# only expands the current page's path and keeps its other links in a hidden,
# flat list — inferring nesting from those put Eon's MongoDB Atlas pages under
# GCP's "Regions" and dropped every section heading.

import json  # noqa: E402

NEW_ROOT = "https://docs.eon.io/data-protection/get-started"
U = "$undefined"


def _page(slug, title, hidden=False):
    return {"type": "page", "id": f"page:{slug}", "title": title, "slug": slug, "hidden": hidden}


def _section(slug, title, children, overview=U):
    return {"type": "section", "id": f"section:{slug}", "title": title, "slug": slug,
            "hidden": False, "overviewPageId": overview, "children": children}


NAV = {"children": [
    {"type": "sidebarGroup", "id": "sidebar-group:1", "children": [
        _page("data-protection/get-started", "Get Started"),
    ]},
    _section("data-protection/cloud-workloads", "Cloud Workloads", [
        _section("data-protection/cloud-workloads/gcp", "GCP", [
            _section("data-protection/cloud-workloads/gcp/resources", "Resources", [
                _page("data-protection/cloud-workloads/gcp/resources/bigquery", "BigQuery"),
            ], overview="pages/data-protection/cloud-workloads/gcp/resources/index.mdx"),
            _page("data-protection/cloud-workloads/gcp/regions", "Regions"),
        ]),
        _section("data-protection/cloud-workloads/mongodb-atlas", "MongoDB Atlas", [
            _section("data-protection/cloud-workloads/mongodb-atlas/resources", "Resources", [
                _page("data-protection/cloud-workloads/mongodb-atlas/resources/mongodb", "MongoDB"),
            ]),
            _page("data-protection/cloud-workloads/mongodb-atlas/secret", "Draft page", hidden=True),
        ]),
    ]),
    {"type": "link", "id": "link:1", "title": "Status", "url": "https://status.eon.io"},
], "currentVariantId": U}


def _flight_page(nav, split=True):
    """A page whose flight payload carries *nav*, split across two pushes the
    way Next.js streams it, plus the hidden flat link list the old code read."""
    blob = 'a:["$","$L111",null,' + json.dumps(nav, separators=(",", ":")) + "]\n"
    cut = len(blob) // 2 if split else len(blob)
    pushes = "".join(
        f"<script>self.__next_f.push([1,{json.dumps(part)}])</script>"
        for part in (blob[:cut], blob[cut:]) if part
    )
    flat = "".join(f'<li><a href="/{p}">{p}</a></li>' for p in (
        "data-protection/get-started", "data-protection/cloud-workloads/gcp/regions",
        "data-protection/cloud-workloads/mongodb-atlas/resources/mongodb"))
    return (f'<html><body><aside class="fern-sidebar-desktop"><nav class="absolute h-px"><ul>{flat}</ul></nav>'
            f'</aside><div class="fern-prose">x</div>{pushes}</body></html>')


async def _toc(html):
    return await FernProfile().build_toc(NEW_ROOT, FakeScraper({}, raw_by_url={NEW_ROOT: html}))


@pytest.mark.asyncio
async def test_the_toc_follows_the_embedded_nav_tree():
    toc = await _toc(_flight_page(NAV))
    assert [(e.level, e.title) for e in toc] == [
        (0, "Get Started"),
        (0, "Cloud Workloads"),
        (1, "GCP"),
        (2, "Resources"),
        (3, "BigQuery"),
        (2, "Regions"),
        (1, "MongoDB Atlas"),
        (2, "Resources"),
        (3, "MongoDB"),
    ]


@pytest.mark.asyncio
async def test_mongodb_is_not_nested_under_gcp_regions():
    """The live regression: URL depth attached MongoDB to the preceding page."""
    toc = await _toc(_flight_page(NAV))
    by_title = {e.title: e for e in toc}
    regions = by_title["Regions"]
    mongodb = by_title["MongoDB"]
    assert mongodb.parent_url != regions.url
    assert regions.level < mongodb.level  # deeper, but under its own section


@pytest.mark.asyncio
async def test_sections_are_headings_unless_they_have_an_overview_page():
    toc = await _toc(_flight_page(NAV))
    gcp = next(e for e in toc if e.title == "GCP")
    gcp_resources = next(e for e in toc if e.title == "Resources" and e.level == 2 and "gcp" in (toc[toc.index(e) + 1].url or ""))
    assert gcp.url is None and gcp.is_article is False
    assert gcp_resources.url == "https://docs.eon.io/data-protection/cloud-workloads/gcp/resources"
    assert gcp_resources.is_article is True


@pytest.mark.asyncio
async def test_hidden_pages_and_external_links_are_skipped():
    toc = await _toc(_flight_page(NAV))
    titles = [e.title for e in toc]
    assert "Draft page" not in titles and "Status" not in titles


@pytest.mark.asyncio
async def test_page_urls_are_absolute_on_the_docs_origin():
    toc = await _toc(_flight_page(NAV))
    assert next(e for e in toc if e.title == "MongoDB").url == (
        "https://docs.eon.io/data-protection/cloud-workloads/mongodb-atlas/resources/mongodb")


@pytest.mark.asyncio
async def test_a_single_unsplit_push_works_too():
    assert len(await _toc(_flight_page(NAV, split=False))) == 9


@pytest.mark.asyncio
async def test_a_malformed_payload_falls_back_to_url_depth():
    """Never worse than before: an unparsable tree uses the old inference."""
    html = _flight_page(NAV)
    first, second = html.index("<script>"), html.rindex("<script>")
    truncated = html[:second] + html[html.index("</script>", second) + len("</script>"):]
    assert first != second  # there really were two pushes to drop one of
    toc = await _toc(truncated)
    assert toc and all(e.url for e in toc)  # the flat-list fallback: no headings
