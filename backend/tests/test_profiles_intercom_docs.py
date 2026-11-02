"""Tests for the intercom_docs profile — Intercom's newer "Docs" help-center
template (help.druva.com since 2026-10/11).

The template ships the help center's complete collection tree in every page's
``__NEXT_DATA__`` (``props.pageProps.v2Navbar.collections``: collections with
``articles`` and nested ``subcollections``), while the DOM carries hashed
CSS-module classes (``DocsArticleBody_body__BYuoi``). Hermetic: canned HTML.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.profiles.detector import detect_platform
from app.services.profiles.intercom import IntercomProfile
from app.services.profiles.intercom_docs import PROFILE, IntercomDocsProfile
from app.services.profiles.scraper import FakeScraper

ROOT = "https://help.druva.com/en/"
H = "https://help.druva.com/en"


def _art(i, title):
    return {"id": str(i), "title": title, "url": f"{H}/articles/{i}-a{i}"}


NAV = [
    {"id": "1", "name": "Druva Cloud Platform", "url": f"{H}/collections/1-dcp",
     "articles": [_art(10, "Release Notes")],
     "subcollections": [
         {"id": "2", "name": "Get Started", "url": f"{H}/collections/2-gs",
          "articles": [_art(11, "Log in"), _art(12, "Navigate the console")],
          "subcollections": [
              {"id": "3", "name": "Accounts", "url": f"{H}/collections/3-acc",
               "articles": [_art(13, "Create an account")], "subcollections": []},
          ]},
     ]},
    {"id": "4", "name": "Knowledge Base", "url": f"{H}/collections/4-kb",
     # An article listed in two collections keeps its first position.
     "articles": [_art(14, "Troubleshooting"), _art(11, "Log in")],
     "subcollections": []},
]


def _page(body: str = "", nav=NAV) -> str:
    data = {"props": {"pageProps": {"v2Navbar": {"collections": nav}}}}
    return f"""<html><head><title>Druva Documentation</title></head><body>
<div id="__next"><div class="DocsShell_grid__w_wa8">
<aside class="DocsSidebar_section__x"><a class="DocsSidebar_collectionHeader__hbgyv" href="{H}/collections/1-dcp">Druva Cloud Platform</a></aside>
<main class="DocsShell_main__bquc5" id="docs-main">{body}</main></div></div>
<script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script>
<script src="https://static.intercomassets.com/x.js"></script></body></html>"""


HOME = _page('<div class="DocsHome_card__WgIjM" data-testid="docs-home-collection-card"></div>')

ARTICLE = _page("""
<div class="DocsArticle_article__C1mpK">
  <nav class="DocsArticle_breadcrumbs__a"><a class="DocsArticle_crumbLink__b" href="/en/">All Collections</a></nav>
  <header class="DocsArticle_header__c">
    <h1 class="DocsArticle_title__3pXkn">Log in to Druva Cloud Platform Console</h1>
    <div class="DocsArticle_metaRow__d">Updated over a week ago</div>
  </header>
  <div class="jsx-1"><article class="jsx-1 DocsArticleBody_body__BYuoi">
    <div class="intercom-interblocks-paragraph"><p>Open the console URL.</p></div>
    <div class="intercom-interblocks-image"><img src="/i/o/shot.png" alt="Login"></div>
  </article></div>
  <div class="DocsArticle_reactions__e">Did this answer your question?</div>
  <div class="DocsArticle_related__f"><a class="DocsArticle_relatedCard__g" href="/en/articles/12">Related article</a></div>
</div>
<nav class="DocsToc_root__h"><a class="DocsToc_link__i" href="#x">On this page</a></nav>
""")


def test_detects_the_docs_template_and_not_the_classic_one():
    assert detect_platform(HOME, ROOT) == "intercom_docs"
    assert PROFILE.detect(ARTICLE, ROOT) is True
    classic = '<html><body><a class="collection-link" href="/en/collections/1">C</a></body></html>'
    assert PROFILE.detect(classic, ROOT) is False
    # ...and the classic profile no longer claims the new template.
    assert IntercomProfile().detect(HOME, ROOT) is False


@pytest.mark.asyncio
async def test_build_toc_walks_the_whole_collection_tree_in_order():
    toc = await IntercomDocsProfile().build_toc(ROOT, FakeScraper({}, raw_by_url={ROOT: HOME}))
    assert [(e.level, e.title, e.is_article) for e in toc] == [
        (0, "Druva Cloud Platform", False),
        (1, "Release Notes", True),
        (1, "Get Started", False),
        (2, "Log in", True),
        (2, "Navigate the console", True),
        (2, "Accounts", False),
        (3, "Create an account", True),
        (0, "Knowledge Base", False),
        (1, "Troubleshooting", True),
        (1, "Log in", True),       # duplicate kept here; extract_source dedupes by URL
    ]
    # Collections are listing pages, not documentation: URL-less headings.
    assert all(e.url is None for e in toc if not e.is_article)
    assert toc[3].url == f"{H}/articles/11-a11"


@pytest.mark.asyncio
async def test_build_toc_is_empty_without_nav_data_or_on_fetch_error():
    page = "<html><body><main>No data</main></body></html>"
    assert await IntercomDocsProfile().build_toc(ROOT, FakeScraper({}, raw_by_url={ROOT: page})) == []
    assert await IntercomDocsProfile().build_toc(ROOT, FakeScraper({}, raw_by_url={})) == []
    broken = HOME.replace('{"props"', '{"props" BROKEN')
    assert await IntercomDocsProfile().build_toc(ROOT, FakeScraper({}, raw_by_url={ROOT: broken})) == []


def test_content_is_title_and_article_body_only():
    url = f"{H}/articles/11-a11"
    html = PROFILE.extract_content_html(ARTICLE, url)
    assert "Log in to Druva Cloud Platform Console" in html
    assert "Open the console URL." in html
    for chrome in ("All Collections", "Updated over a week ago", "Did this answer",
                   "Related article", "On this page", "Druva Cloud Platform</a>"):
        assert chrome not in html
    assert 'src="https://help.druva.com/i/o/shot.png"' in html


def test_content_is_none_without_an_article_body():
    assert PROFILE.extract_content_html(HOME, ROOT) is None


def test_profile_runs_on_raw_http():
    assert PROFILE.content_engine == "raw_http"
