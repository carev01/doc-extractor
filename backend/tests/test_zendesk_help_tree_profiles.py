"""Tests for the zendesk and help_tree profiles (Hornetsecurity / Synology fixes).

  - zendesk    — Zendesk Help Center REST API: category -> sections -> articles,
                 bodies fetched from the article API (raw_http + content_url).
  - help_tree  — rendered Synology-style help tree, scoped to the source URL's
                 /help/<Bundle>/ product out of the shared global nav.

Hermetic: a FakeScraper serves canned API JSON / rendered HTML, no network.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import json

import pytest

from app.services.profiles.scraper import FakeScraper
from app.services.profiles.detector import detect_platform
from app.services.profiles.zendesk import ZendeskProfile
from app.services.profiles.help_tree import HelpTreeProfile, parse_help_tree


# ===========================================================================
# zendesk
# ===========================================================================

ZD_ROOT = "https://support.example.com/hc/en-us/categories/100"
ZD_API = "https://support.example.com/api/v2/help_center/en-us"


def test_zendesk_opts_into_raw_http():
    assert ZendeskProfile().content_engine == "raw_http"


def test_zendesk_detects_on_category_url_even_when_html_is_a_403_shell():
    assert detect_platform("<html><body>403 Forbidden</body></html>", ZD_ROOT) == "zendesk"


def test_zendesk_detects_section_and_article_urls():
    assert ZendeskProfile().detect("", "https://x/hc/en-us/sections/55") is True
    assert ZendeskProfile().detect("", "https://x/hc/en-us/articles/99") is True
    assert ZendeskProfile().detect("", "https://x/something/else") is False


def _zendesk_scraper(*, paginate=False):
    sections = {"sections": [
        {"id": 1, "name": "Getting Started", "position": 0, "parent_section_id": None},
        {"id": 2, "name": "Advanced", "position": 1, "parent_section_id": None},
        {"id": 3, "name": "Advanced Subtopic", "position": 0, "parent_section_id": 2},
    ], "next_page": None}
    articles = {"articles": [
        {"id": 10, "title": "Install", "section_id": 1, "position": 1,
         "html_url": "https://support.example.com/hc/en-us/articles/10-install"},
        {"id": 11, "title": "Welcome", "section_id": 1, "position": 0,
         "html_url": "https://support.example.com/hc/en-us/articles/11-welcome"},
        {"id": 12, "title": "Deep", "section_id": 3, "position": 0,
         "html_url": "https://support.example.com/hc/en-us/articles/12-deep"},
    ], "next_page": None}
    raw = {
        f"{ZD_API}/categories/100/sections.json?per_page=100&page=1": json.dumps(sections),
        f"{ZD_API}/categories/100/articles.json?per_page=100&page=1": json.dumps(articles),
    }
    return FakeScraper({}, raw_by_url=raw)


@pytest.mark.asyncio
async def test_zendesk_builds_ordered_hierarchy_with_api_content_urls():
    toc = await ZendeskProfile().build_toc(ZD_ROOT, _zendesk_scraper())
    shape = [(e.level, e.title, e.url is None) for e in toc]
    assert shape == [
        (0, "Getting Started", True),    # section = url-less structural header
        (1, "Welcome", False),           # articles ordered by position (0 before 1)
        (1, "Install", False),
        (0, "Advanced", True),
        (1, "Advanced Subtopic", True),  # nested child section
        (2, "Deep", False),              # its article, one level deeper
    ]
    install = next(e for e in toc if e.title == "Install")
    assert install.url == "https://support.example.com/hc/en-us/articles/10-install"
    assert install.content_url == f"{ZD_API}/articles/10.json"


@pytest.mark.asyncio
async def test_zendesk_paginates_articles():
    p1 = {"articles": [{"id": 1, "title": "A", "section_id": 1, "position": 0,
                        "html_url": "https://support.example.com/hc/en-us/articles/1-a"}],
          "next_page": f"{ZD_API}/categories/100/articles.json?page=2"}
    p2 = {"articles": [{"id": 2, "title": "B", "section_id": 1, "position": 1,
                        "html_url": "https://support.example.com/hc/en-us/articles/2-b"}],
          "next_page": None}
    raw = {
        f"{ZD_API}/categories/100/sections.json?per_page=100&page=1":
            json.dumps({"sections": [{"id": 1, "name": "S", "position": 0,
                                      "parent_section_id": None}], "next_page": None}),
        f"{ZD_API}/categories/100/articles.json?per_page=100&page=1": json.dumps(p1),
        f"{ZD_API}/categories/100/articles.json?per_page=100&page=2": json.dumps(p2),
    }
    toc = await ZendeskProfile().build_toc(ZD_ROOT, FakeScraper({}, raw_by_url=raw))
    assert [e.title for e in toc if e.url] == ["A", "B"]  # both pages collected


def test_zendesk_extract_content_unwraps_body_and_absolutises_images():
    page = json.dumps({"article": {"body": '<p>Hi</p><img src="/attachments/x.png"/>'}})
    out = ZendeskProfile().extract_content_html(page, "https://support.example.com/hc/en-us/articles/10")
    assert "Hi" in out
    assert 'src="https://support.example.com/attachments/x.png"' in out


def test_zendesk_extract_content_none_without_body():
    assert ZendeskProfile().extract_content_html(json.dumps({"article": {}}), "https://x/") is None
    assert ZendeskProfile().extract_content_html("not json", "https://x/") is None


# --- Help Center root (/hc/<locale>) — all categories in one source (DropSuite) ---

ZD_HC_ROOT = "https://support.example.com/hc/en-us"


def test_zendesk_detects_help_center_root_even_when_html_is_403_shell():
    # DropSuite's root 403s with no usable markers; detection must key on the URL.
    assert detect_platform("<html><body>403</body></html>", ZD_HC_ROOT) == "zendesk"


def _zendesk_root_scraper():
    cats = {"categories": [
        {"id": 2, "name": "Beta", "position": 1},
        {"id": 1, "name": "Alpha", "position": 0},   # out of order -> sorted by position
    ], "next_page": None}

    def cat(cat_id, section, article):
        return {
            f"{ZD_API}/categories/{cat_id}/sections.json?per_page=100&page=1":
                json.dumps({"sections": [section], "next_page": None}),
            f"{ZD_API}/categories/{cat_id}/articles.json?per_page=100&page=1":
                json.dumps({"articles": [article], "next_page": None}),
        }

    raw = {f"{ZD_API}/categories.json?per_page=100&page=1": json.dumps(cats)}
    raw.update(cat(1,
        {"id": 11, "name": "Alpha Sec", "position": 0, "parent_section_id": None},
        {"id": 101, "title": "Alpha Art", "section_id": 11, "position": 0,
         "html_url": "https://support.example.com/hc/en-us/articles/101-a"}))
    raw.update(cat(2,
        {"id": 22, "name": "Beta Sec", "position": 0, "parent_section_id": None},
        {"id": 202, "title": "Beta Art", "section_id": 22, "position": 0,
         "html_url": "https://support.example.com/hc/en-us/articles/202-b"}))
    return FakeScraper({}, raw_by_url=raw)


@pytest.mark.asyncio
async def test_zendesk_root_nests_every_category_in_position_order():
    toc = await ZendeskProfile().build_toc(ZD_HC_ROOT, _zendesk_root_scraper())
    shape = [(e.level, e.title, e.is_article) for e in toc]
    assert shape == [
        (0, "Alpha", False),       # category header (position 0 first)
        (1, "Alpha Sec", False),   # its section, nested one level
        (2, "Alpha Art", True),    # its article, two levels deep
        (0, "Beta", False),
        (1, "Beta Sec", False),
        (2, "Beta Art", True),
    ]
    art = next(e for e in toc if e.title == "Alpha Art")
    assert art.content_url == f"{ZD_API}/articles/101.json"


# ===========================================================================
# help_tree
# ===========================================================================

# A trimmed global nav with two product bundles; the source points at "Prod".
_HELP_TREE_HTML = """
<div id="js-sidebar"><div class="nodes_section">
  <div class="help-tree-node tree_layer_1"><div class="row"><div class="flex">
      <a href="/en-global/DSM/help/Other/other_overview?version=7">Other Product</a></div></div>
    <div class="nodes"><div class="inner">
      <div class="help-tree-node tree_layer_2"><div class="row"><div class="flex">
        <a href="/en-global/DSM/help/Other/other_child?version=7">Other Child</a></div></div></div>
    </div></div></div>
  <div class="help-tree-node tree_layer_1"><div class="row"><div class="flex">
      <a href="/en-global/DSM/help/Prod/prod_overview?version=7">Prod Overview</a></div></div>
    <div class="nodes"><div class="inner">
      <div class="help-tree-node tree_layer_2"><div class="row"><div class="flex">
        <a href="/en-global/DSM/help/Prod/prod_setup?version=7">Setup</a></div></div>
        <div class="nodes"><div class="inner">
          <div class="help-tree-node tree_layer_3"><div class="row"><div class="flex">
            <a href="/en-global/DSM/help/Prod/prod_setup_win?version=7">Windows</a></div></div></div>
        </div></div></div>
    </div></div></div>
</div></div>
"""

PROD_ROOT = "https://kb.example.com/en-global/DSM/help/Prod/prod_overview?version=7"


def test_help_tree_is_not_raw_http():
    # Content is client-rendered -> goes through the Firecrawl render path.
    assert getattr(HelpTreeProfile(), "content_engine", None) is None


def test_help_tree_detects_synology_markers():
    assert detect_platform(_HELP_TREE_HTML, PROD_ROOT) == "help_tree"


def test_help_tree_scopes_to_bundle_with_hierarchy_and_version():
    toc = parse_help_tree(_HELP_TREE_HTML, PROD_ROOT)
    # Only the "Prod" bundle is kept; "Other" is excluded.
    assert [(e.level, e.title) for e in toc] == [
        (0, "Prod Overview"), (1, "Setup"), (2, "Windows"),
    ]
    assert all("/help/Prod/" in e.url for e in toc)
    assert all("version=7" in e.url for e in toc)  # version query preserved
    # Parentage follows the rendered nesting.
    by_title = {e.title: e for e in toc}
    assert by_title["Windows"].parent_url == by_title["Setup"].url
    assert by_title["Setup"].parent_url == by_title["Prod Overview"].url


@pytest.mark.asyncio
async def test_help_tree_build_toc_renders_with_long_wait():
    # build_toc must request a render (long wait), not a raw fetch.
    waits = []

    class _RecordingScraper(FakeScraper):
        async def get_html(self, url, wait_ms=1500):
            waits.append(wait_ms)
            return _HELP_TREE_HTML

    toc = await HelpTreeProfile().build_toc(PROD_ROOT, _RecordingScraper({}))
    assert waits and waits[0] >= 6000  # SPA needs a long wait to build the nav
    assert len(toc) == 3


# A help-page shell like Synology's: the whole #js-sidebar nav and the subheader
# precede the article, and the article body (#kb_help_body) carries an in-body
# .feedBackForm widget as a sibling of the prose.
_HELP_PAGE_HTML = """
<div class="help-page">
  <div class="subheader"><a href="/x">Tab One</a><a href="/y">Tab Two</a></div>
  <aside><div id="js-sidebar">
    <a href="/en-global/DSM/help/Prod/a">Nav Link A</a>
    <a href="/en-global/DSM/help/Prod/b">Nav Link B</a>
  </div></aside>
  <main id="main">
    <div class="content container"><div><div id="kb_help_body" class="kb_accordion_container">
      <div class="kb_accordion">
        <h1>Real Title</h1>
        <p>Real documentation prose about the feature.</p>
        <div class="section"><a href="/en-global/DSM/help/Prod/sub">In-body content link</a></div>
        <div class="feedBackForm feedBackForm--noiframe clearfix en-global">
          <form id="feedback_form"><div id="form_yes_no">Was this article helpful?
            <a href="javascript:void(0)">Yes</a> / <a href="javascript:void(0)">No</a>
            Thank you for the feedback!</div></form>
        </div>
      </div>
    </div></div>
    <div class="section-selector-container"><div class="section-selector"></div></div>
  </main>
</div>
"""


def test_help_tree_content_config_targets_article_body_not_nav():
    cfg = HelpTreeProfile().content_config()
    assert cfg["includeTags"] == ["#kb_help_body"]      # not div.help-page (wraps the nav)
    assert ".feedBackForm" in cfg["excludeTags"]        # in-body feedback widget


def test_help_tree_content_scopes_out_nav_and_feedback():
    from app.services.profiles.content_scope import scope_content_html
    cfg = HelpTreeProfile().content_config()
    out = scope_content_html(
        _HELP_PAGE_HTML, "https://kb.example.com/en-global/DSM/help/Prod/page",
        cfg["includeTags"], cfg["excludeTags"],
    )
    assert "Real documentation prose" in out
    assert "In-body content link" in out          # genuine in-article links kept
    assert "Nav Link A" not in out                # sidebar dropped
    assert "Tab One" not in out                   # subheader dropped
    assert "Was this article helpful" not in out  # feedback widget dropped
    assert "Thank you for the feedback" not in out
