"""Tests for the warm-up + list-group support-manuals profile (WAF warm-up +
CSS-collapsed Bootstrap list-group TOC)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.services.profiles.warmup_listgroup import (
    WarmupListGroupProfile,
    _to_en_us,
    parse_listgroup_toc,
)
from app.services.profiles.scraper import FakeScraper

ROOT = ("https://www.dell.com/support/manuals/en-us/"
        "enterprise-copy-data-management/pp-dm_20.1_ag")

# Mirrors the real list-group: a leaf, then a parent section that is ALSO a
# page (its own <a>) wrapping a nested <ul class="collapse"> of children, one of
# which is itself a parent with a grandchild.
TOC_HTML = """
<ul id="toc-main-parent-ul" class="nav-list list-group list-group-flush">
  <li class="list-group-item">
    <a class="list-group-link toc-child"
       href="//www.dell.com/support/manuals/pt-br/enterprise-copy-data-management/pp-dm_20.1_ag/prefacio?guid=guid-aaa&amp;lang=pt-br">Preface</a>
  </li>
  <li class="list-group-item" id="LIguid-bbb">
    <a class="list-group-link toc-child"
       href="//www.dell.com/support/manuals/pt-br/enterprise-copy-data-management/pp-dm_20.1_ag/getting-started?guid=guid-bbb&amp;lang=pt-br">Getting started</a>
    <div role="heading"><button class="list-group-btn open-toc-menu collapsed">x</button></div>
    <ul class="list-group collapse" id="childOfguid-bbb">
      <li class="list-group-item">
        <a class="list-group-link"
           href="//www.dell.com/support/manuals/pt-br/enterprise-copy-data-management/pp-dm_20.1_ag/overview?guid=guid-ccc&amp;lang=pt-br">Overview</a>
      </li>
      <li class="list-group-item" id="LIguid-ddd">
        <a class="list-group-link"
           href="//www.dell.com/support/manuals/pt-br/enterprise-copy-data-management/pp-dm_20.1_ag/deployment?guid=guid-ddd&amp;lang=pt-br">Deployment</a>
        <div role="heading"><button class="open-toc-menu collapsed">x</button></div>
        <ul class="list-group collapse" id="childOfguid-ddd">
          <li class="list-group-item">
            <a class="list-group-link"
               href="//www.dell.com/support/manuals/pt-br/enterprise-copy-data-management/pp-dm_20.1_ag/sizing?guid=guid-eee&amp;lang=pt-br">Sizing</a>
          </li>
        </ul>
      </li>
    </ul>
  </li>
</ul>
"""


def test_detect_matches_manuals_by_url():
    # Root HTML is the WAF block page in practice, so detection is URL-based.
    assert WarmupListGroupProfile().detect("<html>Access Denied</html>", ROOT) is True


def test_detect_rejects_other_paths_and_other_hosts():
    assert WarmupListGroupProfile().detect("", "https://www.dell.com/support/home/en-us") is False
    assert WarmupListGroupProfile().detect("", "https://docs.other.com/support/manuals/x") is False


def test_to_en_us_forces_path_and_lang_param():
    src = ("//www.dell.com/support/manuals/pt-br/enterprise-copy-data-management/"
           "pp-dm_20.1_ag/getting-started?guid=guid-bbb&lang=pt-br")
    out = _to_en_us(src, ROOT)
    assert out.startswith("https://www.dell.com/support/manuals/en-us/")
    assert "lang=en-us" in out and "lang=pt-br" not in out
    # The stable guid key is preserved.
    assert "guid=guid-bbb" in out


def test_to_en_us_adds_lang_to_base_without_query():
    out = _to_en_us(ROOT, ROOT)
    assert out.endswith("pp-dm_20.1_ag?lang=en-us")


def test_parse_toc_hierarchy_and_normalization():
    toc = parse_listgroup_toc(TOC_HTML, ROOT)
    by_title = {e.title: e for e in toc}
    assert set(by_title) == {"Preface", "Getting started", "Overview",
                             "Deployment", "Sizing"}
    # Levels follow <li> nesting.
    assert by_title["Preface"].level == 0
    assert by_title["Getting started"].level == 0
    assert by_title["Overview"].level == 1
    assert by_title["Deployment"].level == 1
    assert by_title["Sizing"].level == 2
    # Parent links thread through (sections are also pages).
    assert by_title["Overview"].parent_url == by_title["Getting started"].url
    assert by_title["Sizing"].parent_url == by_title["Deployment"].url
    # Every node is an article with an en-us, lang-forced URL.
    assert all(e.is_article for e in toc)
    assert all("lang=en-us" in e.url and "/en-us/" in e.url for e in toc)


@pytest.mark.asyncio
async def test_build_toc_uses_warmup_render():
    toc_url = _to_en_us(ROOT, ROOT)
    scraper = FakeScraper({}, warmup_render_by_url={toc_url: {"outerHtml": TOC_HTML}})
    toc = await WarmupListGroupProfile().build_toc(ROOT, scraper)
    assert [e.title for e in toc][:2] == ["Preface", "Getting started"]
    assert len(toc) == 5


def test_browserless_content_spec():
    spec = WarmupListGroupProfile().browserless_content_spec()
    assert spec["selector"] == "#divTopicContent"
    assert spec["warmup_url"].startswith("https://www.dell.com")


def test_parses_real_fixture():
    path = os.path.join(os.path.dirname(__file__), "fixtures", "platforms", "warmup_listgroup.html")
    if not os.path.exists(path):
        pytest.skip("real list-group fixture not present")
    html = open(path, encoding="utf-8").read()
    toc = parse_listgroup_toc(html, ROOT)
    # The captured guide has 363 anchor entries; every one normalised to en-us.
    assert len(toc) == 363
    assert all("/en-us/" in e.url and "lang=en-us" in e.url for e in toc)
    assert all("guid=" in e.url for e in toc)
    # Hierarchy was captured (there are nested sections).
    assert max(e.level for e in toc) >= 2
    assert any(e.parent_url for e in toc)
