"""Tests for the generic static-HTML content scoper (content_scope.py).

Covers the raw_http content engine's selector-based body extraction used by
profiles that don't define their own ``extract_content_html`` (mkdocs,
docusaurus, lazy_tree, confluence, flare_html5, LLM-derived sources).

Selector handling is deliberately bs4-native for attribute-presence and id
selectors (soupsieve's compiled-selector cache proved unreliable for
attribute-presence selectors under the full suite — see PR #62); these tests
lock that behaviour so it can't regress to a flaky ``select_one``.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.profiles.content_scope import scope_content_html

BASE = "https://docs.example.com/guide/install/"


def test_scopes_by_class_selector():
    html = (
        "<html><body><nav>Home Search</nav>"
        '<article class="md-content__inner"><h1>Install</h1>'
        "<p>Run the installer.</p></article></body></html>"
    )
    out = scope_content_html(html, BASE, ["article.md-content__inner"])
    assert "Run the installer." in out
    assert "Home Search" not in out  # nav outside the body is dropped


def test_scopes_by_id_selector():
    html = '<html><body><header>chrome</header><div id="doc"><p>real body</p></div></body></html>'
    out = scope_content_html(html, BASE, ["#doc"])
    assert "real body" in out
    assert "chrome" not in out


def test_scopes_by_attribute_presence_selector():
    """[data-mc-content-body] is resolved bs4-native (find), not via soupsieve."""
    html = (
        '<html><body><header>nav</header>'
        '<div data-mc-content-body="True"><p>flare body</p></div></body></html>'
    )
    out = scope_content_html(html, BASE, ["[data-mc-content-body]"])
    assert "flare body" in out
    assert "nav" not in out


def test_scopes_by_attribute_value_selector():
    html = '<html><body><div role="main"><p>main body</p></div><div>other</div></body></html>'
    out = scope_content_html(html, BASE, ["[role=main]"])
    assert "main body" in out
    assert "other" not in out


def test_keeps_union_of_all_include_matches():
    """Mirrors category_accordion: the body is an <article> plus sibling
    .m.embed table blocks outside it — both selectors' matches are kept."""
    html = (
        "<body><nav>menu</nav>"
        '<article class="article"><p>prose</p></article>'
        '<div class="m embed"><table><tr><td>cell</td></tr></table></div>'
        "</body>"
    )
    out = scope_content_html(html, BASE, ["article.article", ".m.embed"])
    assert "prose" in out and "cell" in out  # both subtrees kept
    assert "menu" not in out


def test_exclude_removes_a_matched_root():
    """An excluded element that is itself a matched include root (e.g. a sidebar
    sharing the .m.embed class) is dropped, not just its descendants."""
    html = (
        '<article class="article"><p>keep</p></article>'
        '<div class="m embed category-sidebar"><a>side link</a></div>'
    )
    out = scope_content_html(
        html, BASE, ["article.article", ".m.embed"], [".category-sidebar"]
    )
    assert "keep" in out
    assert "side link" not in out


def test_drops_nested_duplicate_matches():
    """A match nested inside another match isn't double-counted."""
    html = '<div id="doc"><article class="article"><p>body</p></article></div>'
    out = scope_content_html(html, BASE, ["#doc", "article.article"])
    assert out.count("<p>body</p>") == 1


def test_returns_none_when_no_include_matches():
    html = "<html><body><header>only nav</header></body></html>"
    assert scope_content_html(html, BASE, ["#doc", ".wiki-content"]) is None


def test_drops_exclude_selectors():
    html = (
        '<div class="theme-doc-markdown"><p>keep this</p>'
        '<a class="GoToTop">top</a><div class="feedback-button">rate</div></div>'
    )
    out = scope_content_html(
        html, BASE, [".theme-doc-markdown"], [".GoToTop", ".feedback-button"]
    )
    assert "keep this" in out
    assert "top" not in out
    assert "rate" not in out


def test_absolutises_relative_images():
    html = (
        '<div id="doc"><img src="img/a.png"/><img src="/static/b.png"/>'
        '<img src="https://cdn.example.com/c.png"/></div>'
    )
    out = scope_content_html(html, BASE, ["#doc"])
    assert "https://docs.example.com/guide/install/img/a.png" in out
    assert "https://docs.example.com/static/b.png" in out
    assert "https://cdn.example.com/c.png" in out  # already absolute, untouched
