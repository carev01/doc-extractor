"""Tests for the aem_guides profile (Adobe Experience Manager Guides; e.g. Arctera).

Every page carries the guide's complete nested nav under ``nav.cmp-navigation``
(AEM Core Components), and the topic body is server-rendered in
``main.topic-renderer`` — so the whole profile runs on raw_http.

Hermetic: a FakeScraper serves canned HTML, no network.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from app.services.profiles.scraper import FakeScraper
from app.services.profiles.detector import detect_platform
from app.services.profiles.aem_guides import PROFILE, AemGuidesProfile

BASE = ("https://docs.backupexec.com/us/en/backupexec-userdocs/product-guides/"
        "all-platforms/backup-exec-administrators-guide/25-1/")
ROOT = BASE + "introducing-backup-exec.html"
P = "/us/en/backupexec-userdocs/product-guides/all-platforms/backup-exec-administrators-guide/25-1/"


def _item(level, href, title, children=""):
    group = f'<ul class="cmp-navigation__group">{children}</ul>' if children else ""
    return (
        f'<li class="cmp-navigation__item cmp-navigation__item--level-{level}">'
        f'<div class="cmp-navigation__item-container">'
        f'<i class="fa fas fa-angle-down cmp-navigation__item-arrow-icon"></i>'
        f'<a class="cmp-navigation__item-link" href="{P}{href}">{title}</a></div>'
        f"{group}</li>"
    )


NAV = (
    '<nav class="cmp-navigation"><ul class="cmp-navigation__group">'
    + _item(0, "introducing-backup-exec.html", "Introducing Backup Exec",
            _item(1, "introducing-backup-exec/about.html", "About Backup Exec"))
    + _item(0, "installation.html", "Installation",
            _item(1, "installation/methods.html", "Methods for installing the Agent",
                  _item(2, "installation/methods/push.html", "Push-installing the Agent")))
    + "</ul></nav>"
)

PAGE = f"""
<html lang="en"><head><meta name="last-modified" content="2026-04-02T15:17:57.906Z"></head>
<body>
  <div class="version-switcher"><a class="version-switcher__link" href="{P}introducing-backup-exec.html">25.1</a></div>
  <section class="navigation-section"><div id="cmp-navigation-wrapper" class="cmp-navigation-wrapper">{NAV}</div></section>
  <main class="topic-renderer" id="section-topic" role="main">
    <div class="topic-renderer__parsys"><div class="topic-renderer__wrapper">
      <div class="topic-renderer__breadcrumb"><nav class="cmp-breadcrumb"><a href="/us/en/home.html">Home</a></nav></div>
      <header class="topic-renderer__header">
        <h1 id="topic-title">Introducing Backup Exec</h1>
        <div>Table of Contents PDF Export Options This Topic Entire Document Share</div>
      </header>
      <div class="topic-renderer__meta">Last published : Apr 02, 2026</div>
      <div class="topic-renderer__toolbar topic-renderer__toolbar--desktop"></div>
      <div class="topic-renderer__content">
        <p>Backup Exec is a high-performance data management solution.</p>
        <img src="/content/dam/backupexec/figure.png" alt="Architecture">
      </div>
    </div></div>
  </main>
</body></html>
"""


def _scraper():
    return FakeScraper({}, raw_by_url={ROOT: PAGE})


def test_detects_an_aem_guides_page():
    assert detect_platform(PAGE, ROOT) == "aem_guides"


def test_does_not_detect_a_plain_aem_site_without_the_topic_renderer():
    plain = f"<html><body>{NAV}<main><p>Marketing page</p></main></body></html>"
    assert PROFILE.detect(plain, ROOT) is False


@pytest.mark.asyncio
async def test_build_toc_reads_the_nested_nav_in_order():
    toc = await AemGuidesProfile().build_toc(ROOT, _scraper())
    assert [(e.level, e.title) for e in toc] == [
        (0, "Introducing Backup Exec"),
        (1, "About Backup Exec"),
        (0, "Installation"),
        (1, "Methods for installing the Agent"),
        (2, "Push-installing the Agent"),
    ]
    push = toc[-1]
    assert push.url == BASE + "installation/methods/push.html"
    assert push.parent_url == BASE + "installation/methods.html"
    assert push.is_article is True
    # A section parent is a real page too: it keeps its URL.
    assert toc[2].url == BASE + "installation.html" and toc[2].is_article is False


@pytest.mark.asyncio
async def test_build_toc_returns_empty_when_the_fetch_fails():
    toc = await AemGuidesProfile().build_toc(ROOT, FakeScraper({}, raw_by_url={}))
    assert toc == []


def test_content_is_the_title_and_topic_body_only():
    html = PROFILE.extract_content_html(PAGE, ROOT)
    assert "<h1" in html and "Introducing Backup Exec" in html
    assert "high-performance data management" in html
    for chrome in ("Table of Contents PDF Export", "Last published", "Home",
                   "Push-installing", "25.1"):
        assert chrome not in html


def test_content_absolutises_image_urls():
    html = PROFILE.extract_content_html(PAGE, ROOT)
    assert 'src="https://docs.backupexec.com/content/dam/backupexec/figure.png"' in html


def test_content_is_none_without_a_topic_body():
    assert PROFILE.extract_content_html("<html><body><main></main></body></html>", ROOT) is None


def test_profile_runs_raw_http_gently_and_templates_slug_versions():
    assert PROFILE.content_engine == "raw_http"
    # The portal answered 429 after four requests in ~5s; one request every
    # ~3s never tripped it.
    assert PROFILE.raw_http_concurrency == 1
    assert PROFILE.raw_http_request_delay >= 1.5
    assert PROFILE.slug_versions is True
