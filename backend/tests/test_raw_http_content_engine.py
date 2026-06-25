"""Tests for the generalized raw_http content engine wiring.

Covers:
  - the static profiles opted into raw_http expose content_engine = "raw_http";
  - _resolve_content_engine precedence (per-source profile_config override beats
    / supplies the engine, enabling LLM-derived sources to opt in via data);
  - end-to-end body scoping via each profile's content_config selectors (the
    path _scrape_via_raw_http uses for profiles without extract_content_html);
  - the failure-rate abort decision (_raw_failure_exceeded).
"""

import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.firecrawl import _resolve_content_engine, _raw_failure_exceeded
from app.services.profiles.content_scope import scope_content_html
from app.services.profiles.category_accordion import CategoryAccordionProfile
from app.services.profiles.confluence import ConfluenceProfile
from app.services.profiles.docusaurus import DocusaurusProfile
from app.services.profiles.flare_html5 import FlareHtml5Profile
from app.services.profiles.lazy_tree import LazyTreeProfile
from app.services.profiles.mkdocs import MkDocsProfile
from app.core.config import settings


# ---------------------------------------------------------------------------
# content_engine opt-in
# ---------------------------------------------------------------------------

def test_static_profiles_opt_into_raw_http():
    for P in (FlareHtml5Profile, MkDocsProfile, ConfluenceProfile,
              DocusaurusProfile, LazyTreeProfile, CategoryAccordionProfile):
        assert P().content_engine == "raw_http", P.__name__


# ---------------------------------------------------------------------------
# _resolve_content_engine — per-source override precedence
# ---------------------------------------------------------------------------

class _Source:
    def __init__(self, profile_config=None):
        self.profile_config = profile_config


class _ProfileNoEngine:
    pass


class _ProfileRawEngine:
    content_engine = "raw_http"


def test_resolve_uses_profile_attribute_when_no_override():
    assert _resolve_content_engine(_Source(), _ProfileRawEngine()) == "raw_http"
    assert _resolve_content_engine(_Source(), _ProfileNoEngine()) is None


def test_resolve_per_source_override_supplies_engine_for_derived():
    # DerivedProfile (no content_engine attr) opts in via profile_config — this
    # is how the LLM-derived category_accordion sources are flipped without code.
    src = _Source({"content_engine": "raw_http", "llm_spec": {"strategy": "sidebar"}})
    assert _resolve_content_engine(src, _ProfileNoEngine()) == "raw_http"


def test_resolve_override_takes_precedence_over_profile():
    src = _Source({"content_engine": "raw_http"})
    assert _resolve_content_engine(src, _ProfileNoEngine()) == "raw_http"


def test_resolve_none_when_neither_set():
    assert _resolve_content_engine(_Source({}), _ProfileNoEngine()) is None
    assert _resolve_content_engine(_Source(None), _ProfileNoEngine()) is None


# ---------------------------------------------------------------------------
# Generic scoping via each profile's real content_config selectors
# ---------------------------------------------------------------------------

def _scope_with(profile, html, url="https://docs.example.com/page/"):
    cfg = profile.content_config()
    return scope_content_html(
        html, url, cfg.get("includeTags") or [], cfg.get("excludeTags") or []
    )


def test_mkdocs_selector_scopes_body():
    html = '<body><nav>N</nav><article class="md-content__inner"><p>mk body</p></article></body>'
    out = _scope_with(MkDocsProfile(), html)
    assert "mk body" in out and "N" not in out


def test_docusaurus_selector_scopes_body():
    html = '<body><aside>side</aside><div class="theme-doc-markdown"><p>docu body</p></div></body>'
    out = _scope_with(DocusaurusProfile(), html)
    assert "docu body" in out and "side" not in out


def test_lazy_tree_selector_scopes_body():
    html = '<body><header>h</header><div id="doc"><p>lazy body</p></div></body>'
    out = _scope_with(LazyTreeProfile(), html)
    assert "lazy body" in out and "h" not in out


def test_confluence_selector_scopes_body():
    html = '<body><div class="aui-nav">nav</div><div class="wiki-content"><p>conf body</p></div></body>'
    out = _scope_with(ConfluenceProfile(), html)
    assert "conf body" in out and "nav" not in out


def test_flare_html5_attribute_selector_scopes_body():
    html = '<body><header>nav</header><div data-mc-content-body="True"><p>h5 body</p></div></body>'
    out = _scope_with(FlareHtml5Profile(), html)
    assert "h5 body" in out and "nav" not in out


def test_category_accordion_keeps_article_and_embed_drops_sidebar():
    # Body = <article class="m article"> + sibling .m.embed table; the .m.embed
    # sidebar is removed via excludeTags (.category-sidebar).
    html = (
        '<body><article class="m article"><p>accordion body</p></article>'
        '<div class="m embed"><table><tr><td>tbl</td></tr></table></div>'
        '<div class="m embed category-sidebar"><a>nav link</a></div></body>'
    )
    out = _scope_with(CategoryAccordionProfile(), html)
    assert "accordion body" in out and "tbl" in out
    assert "nav link" not in out


# ---------------------------------------------------------------------------
# Failure-rate abort decision (defaults: min_attempts=10, max_rate=0.3)
# ---------------------------------------------------------------------------

def test_failure_guard_aborts_above_threshold():
    assert settings.raw_http_min_attempts == 10
    assert settings.raw_http_max_failure_rate == 0.3
    assert _raw_failure_exceeded(attempted=10, failed=4) is True   # 40% > 30%


def test_failure_guard_allows_at_or_below_threshold():
    assert _raw_failure_exceeded(attempted=10, failed=3) is False  # 30% not > 30%
    assert _raw_failure_exceeded(attempted=100, failed=10) is False  # 10%


def test_failure_guard_ignores_small_samples():
    # Below min_attempts we never abort, even at 100% failure.
    assert _raw_failure_exceeded(attempted=9, failed=9) is False
