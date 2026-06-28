"""Tests for the rspress profile (full nav tree in static HTML, e.g. AvePoint Learn).

Rspress server-renders the complete sidebar (aside.rspress-sidebar) and the
article body (.rspress-doc) into every page's static HTML, then collapses the
sidebar to the current page after JS hydration -> we run on the raw_http path.

Hermetic: a FakeScraper serves canned HTML, no network.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.profiles.content_scope import scope_content_html
from app.services.profiles.rspress import RspressProfile

ROOT = "https://learn.avepoint.com/m365/about-cloud-backup.html"

# Representative Rspress page: rspress-sidebar (flat <h2>/<a>, no <ul>/<li>),
# a logo anchor to exclude, and an rspress-doc body with chrome to drop.
PAGE = """
<html><body>
  <nav class="rspress-nav"><a class="logo" href="/index.html">AvePoint Learn</a></nav>
  <aside class="sidebar_dd719 rspress-sidebar">
    <div class="logo-wrap"><a href="/index.html">AvePoint Learn</a></div>
    <div class="menu">
      <h2>About Cloud Backup</h2>
      <a href="/m365/about-cloud-backup/express.html"><div class="menuItem_ac22e">Express</div></a>
      <h2>Cloud Backup</h2>
      <a href="/m365/about-cloud-backup/cloud-backup/multigeo.html"><div class="menuItem_ac22e">Multi-Geo Support</div></a>
      <a href="/m365/whats-new.html"><div class="menuItem_ac22e">What's New</div></a>
      <h2>FAQs</h2>
      <a href="/m365/faqs/license.html"><div class="menuItem_ac22e">License and Subscription</div></a>
      <a href="/m365/faqs/storage.html"><div class="menuItem_ac22e">Storage</div></a>
    </div>
  </aside>
  <div class="rspress-doc">
    <h1>About Cloud Backup</h1>
    <nav class="in-doc-breadcrumb">Home / About</nav>
    <div><p>Cloud Backup ensures resiliency of service.</p></div>
    <div class="rspress-local-toc-container">On this page</div>
    <footer class="rspress-doc-footer">Previous Next Edit this page</footer>
  </div>
</body></html>
"""


def test_opts_into_raw_http():
    assert RspressProfile().content_engine == "raw_http"


def test_detect_needs_both_hooks():
    prof = RspressProfile()
    assert prof.detect(PAGE, ROOT) is True
    assert prof.detect('<div class="rspress-doc"></div>', ROOT) is False
    assert prof.detect('<aside class="rspress-sidebar"></aside>', ROOT) is False
    assert prof.detect("<html><body><p>hi</p></body></html>", "https://x/") is False


def test_content_scopes_doc_and_drops_chrome():
    cfg = RspressProfile().content_config()
    out = scope_content_html(PAGE, ROOT, cfg["includeTags"], cfg["excludeTags"])
    assert "resiliency of service" in out      # body kept
    assert "About Cloud Backup" in out         # h1 kept
    assert "On this page" not in out           # local TOC dropped
    assert "Edit this page" not in out         # footer dropped
    assert "Home / About" not in out           # in-doc nav dropped
    assert "License and Subscription" not in out  # sidebar outside scope
