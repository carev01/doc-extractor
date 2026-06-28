# Rspress Extraction Profile Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a generic `rspress` extraction profile that extracts Rspress-based documentation sites (validated on AvePoint Learn) via the raw-HTTP path.

**Architecture:** A new self-registering `ExtractionProfile` (`backend/app/services/profiles/rspress.py`) with `content_engine = "raw_http"`. It detects the Rspress fingerprint, parses the full server-rendered sidebar (`aside.rspress-sidebar`) from raw HTML into an ordered, nested TOC, and scopes article bodies to `.rspress-doc` — all on the existing raw-HTTP content path (plain GET + BeautifulSoup, no JS/Firecrawl/Browserless). No pipeline, schema, or config changes.

**Tech Stack:** Python 3, BeautifulSoup4, pytest (+ pytest-asyncio). Backend lives in `backend/`; run all commands from `backend/`.

## Global Constraints

- Profile module is `backend/app/services/profiles/rspress.py`; profile `name = "rspress"`; class `RspressProfile`; module-level `PROFILE = RspressProfile()` registered via `registry.register(PROFILE)`.
- `content_engine = "raw_http"` (class attribute) — content scrape uses plain GET + `scope_content_html`, never JS render.
- `detect()` returns True only when **both** `"rspress-doc"` and `"rspress-sidebar"` are present in the root HTML.
- TOC hierarchy: article `level` = URL path depth relative to the guide root (longest common directory prefix of the sidebar's article URLs); section `<h2>` headers become structural nodes `url=None, is_article=False` at `(next article level − 1)`, clamped ≥ 0. Entries are emitted in sidebar DOM order (DFS pre-order). `parent_url` is left as the default `None` (the pipeline's `_resolve_toc_parents` nests by level).
- The site logo / cross-guide anchors (paths not under the guide's top path segment) are excluded from the TOC.
- `content_config()` returns `includeTags=[".rspress-doc"]`, `excludeTags=[".rspress-local-toc-container", ".rspress-doc-footer", "nav"]`, `onlyMainContent=False`.
- Tests are hermetic: use `FakeScraper({}, raw_by_url={...})` from `app.services.profiles.scraper`; no network. Mirror the style of `backend/tests/test_profiles_prerendered_toc.py`.
- Do not modify the worker, pipeline, config, schema, or any other profile.

---

### Task 1: Profile shell — detect, content_config, content_engine

Create the profile class with everything except the real `build_toc` (a stub returning `[]` for now), plus its hermetic tests. This task is the detection + content-scoping contract; the parser lands in Task 2.

**Files:**
- Create: `backend/app/services/profiles/rspress.py`
- Test: `backend/tests/test_profiles_rspress.py`

**Interfaces:**
- Consumes: `app.services.profiles.base.TocEntry`, `app.services.profiles.registry`, `app.services.profiles.content_scope.scope_content_html(raw_html, url, include_selectors, exclude_selectors) -> str | None`.
- Produces: `RspressProfile` with attributes `name = "rspress"`, `content_engine = "raw_http"`; methods `detect(self, root_html: str, root_url: str) -> bool`, `content_config(self) -> dict`, and `async build_toc(self, root_url: str, scraper) -> list[TocEntry]` (stub in this task). Module-level `PROFILE = RspressProfile()` (registered in Task 2).

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_profiles_rspress.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_profiles_rspress.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.profiles.rspress'`.

- [ ] **Step 3: Create the profile module (stub build_toc)**

Create `backend/app/services/profiles/rspress.py`:

```python
"""Rspress static-docs profile (full nav tree + bodies pre-rendered into HTML).

Targets documentation sites built on the Rspress framework (rspress.dev), whose
entire sidebar nav AND article bodies are server-rendered into every page's
static HTML. After JS hydration Rspress collapses the sidebar to the current
page, so a rendered scrape sees only one nav link; the full tree exists only in
the raw HTML. Both the tree and the bodies are static, so the profile runs
entirely on the raw_http path (plain GET + local scoping, no render).

Validated against AvePoint Learn (learn.avepoint.com); keyed on the generic
Rspress fingerprint, not the vendor.
"""

from app.services.profiles import registry
from app.services.profiles.base import TocEntry


class RspressProfile:
    name = "rspress"
    # Whole tree + bodies are static HTML; fetch directly, no render.
    content_engine = "raw_http"

    def detect(self, root_html: str, root_url: str) -> bool:
        # The sidebar nav hook plus the article hook together are distinctive to
        # Rspress; either alone is too generic.
        return "rspress-doc" in root_html and "rspress-sidebar" in root_html

    async def build_toc(self, root_url: str, scraper) -> list[TocEntry]:
        return []  # implemented in Task 2

    def content_config(self) -> dict:
        return {
            "includeTags": [".rspress-doc"],
            # Drop the right-rail "on this page" TOC, the prev/next + edit
            # footer, and any in-doc breadcrumb/nav.
            "excludeTags": [".rspress-local-toc-container", ".rspress-doc-footer", "nav"],
            "onlyMainContent": False,
        }


PROFILE = RspressProfile()
# Registered in profiles/__init__.py in Task 2 (once build_toc is implemented).
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_profiles_rspress.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/profiles/rspress.py backend/tests/test_profiles_rspress.py
git commit -m "feat(profiles): rspress profile shell — detect + content_config (raw_http)"
```

---

### Task 2: build_toc — parse the Rspress sidebar into an ordered nested TOC

Implement the real `build_toc` parser and register the profile so auto-detection picks it up.

**Files:**
- Modify: `backend/app/services/profiles/rspress.py` (replace the `build_toc` stub; add imports)
- Modify: `backend/app/services/profiles/__init__.py` (add the registration import)
- Test: `backend/tests/test_profiles_rspress.py` (add build_toc + registry + parent-resolution tests)

**Interfaces:**
- Consumes: `scraper.get_raw(url) -> str` (async; `FakeScraper` raises `FileNotFoundError` for unknown URLs and the stub returns it from `raw_by_url`); `bs4.BeautifulSoup`; `urllib.parse.urljoin/urlparse`; `posixpath`; `app.services.profiles.detector.detect_platform(root_html, root_url) -> str | None`; `app.services.firecrawl._resolve_toc_parents(entries: list[dict]) -> list[int | None]`.
- Produces: `RspressProfile.build_toc(root_url, scraper) -> list[TocEntry]` returning entries in DOM order; article entries have a URL and `is_article=True`; `<h2>` section entries have `url=None` and `is_article=False`.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_profiles_rspress.py`:

```python
import pytest

from app.services.profiles.scraper import FakeScraper
from app.services.profiles.detector import detect_platform
from app.services.firecrawl import _resolve_toc_parents


def _scraper():
    return FakeScraper({}, raw_by_url={ROOT: PAGE})


def test_detects_via_registry():
    # Requires the registration added in this task.
    assert detect_platform(PAGE, ROOT) == "rspress"


@pytest.mark.asyncio
async def test_builds_nested_tree_in_order():
    toc = await RspressProfile().build_toc(ROOT, _scraper())
    shape = [(e.level, e.title, e.is_article) for e in toc]
    assert shape == [
        (0, "About Cloud Backup", False),          # h2 section, url=None
        (1, "Express", True),                      # /m365/about-cloud-backup/express.html
        (1, "Cloud Backup", False),                # nested h2 section
        (2, "Multi-Geo Support", True),            # depth-3 path -> level 2
        (0, "What's New", True),                   # /m365/whats-new.html
        (0, "FAQs", False),                        # h2 section
        (1, "License and Subscription", True),
        (1, "Storage", True),
    ]


@pytest.mark.asyncio
async def test_logo_and_cross_guide_anchors_excluded():
    toc = await RspressProfile().build_toc(ROOT, _scraper())
    assert all(e.url != "https://learn.avepoint.com/index.html" for e in toc)
    # Every article entry's path is under the guide root /m365/.
    arts = [e for e in toc if e.is_article]
    assert arts and all("/m365/" in e.url for e in arts)


@pytest.mark.asyncio
async def test_section_nodes_are_structural():
    toc = await RspressProfile().build_toc(ROOT, _scraper())
    faqs = next(e for e in toc if e.title == "FAQs")
    assert faqs.is_article is False
    assert faqs.url is None
    # Articles always carry a URL (so the pipeline scrapes them).
    assert all(e.url for e in toc if e.is_article)


@pytest.mark.asyncio
async def test_parents_resolve_section_as_parent():
    toc = await RspressProfile().build_toc(ROOT, _scraper())
    entries = [
        {"title": e.title, "url": e.url, "level": e.level,
         "is_article": e.is_article, "parent_url": e.parent_url}
        for e in toc
    ]
    parents = _resolve_toc_parents(entries)
    idx = {e["title"]: i for i, e in enumerate(entries)}
    # FAQs children nest under the FAQs section node.
    assert parents[idx["License and Subscription"]] == idx["FAQs"]
    assert parents[idx["Storage"]] == idx["FAQs"]
    # Multi-Geo nests under the nested "Cloud Backup" section.
    assert parents[idx["Multi-Geo Support"]] == idx["Cloud Backup"]
    # Top-level entries have no parent.
    assert parents[idx["What's New"]] is None


@pytest.mark.asyncio
async def test_missing_sidebar_returns_empty():
    s = FakeScraper({}, raw_by_url={ROOT: "<html><body><div class='rspress-doc'>x</div></body></html>"})
    assert await RspressProfile().build_toc(ROOT, s) == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_profiles_rspress.py -v`
Expected: FAIL — `test_detects_via_registry` (rspress not registered → `detect_platform` returns `None`) and the `build_toc` tests (stub returns `[]`, so shape/parent assertions fail).

- [ ] **Step 3: Implement build_toc**

In `backend/app/services/profiles/rspress.py`, add imports at the top (below the docstring) and replace the `build_toc` stub:

```python
import posixpath
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from app.services.profiles import registry
from app.services.profiles.base import TocEntry
```

Replace the stub method body with:

```python
    async def build_toc(self, root_url: str, scraper) -> list[TocEntry]:
        try:
            raw = await scraper.get_raw(root_url)
        except Exception:
            return []
        soup = BeautifulSoup(raw or "", "html.parser")
        side = soup.select_one("aside.rspress-sidebar")
        if not side:
            return []

        # The guide's top path segment (e.g. "/m365/") — used to exclude the
        # site logo ("/index.html") and links into other guides.
        root_segs = [s for s in urlparse(root_url).path.split("/") if s]
        top = "/" + root_segs[0] + "/" if root_segs else "/"

        # Pass 1: ordered nodes (section <h2> headers + in-guide menu anchors).
        # kind is "h2" (title only) or "a" (title + absolute url).
        nodes: list[tuple[str, str, str | None]] = []
        for el in side.find_all(["h2", "a"]):
            if el.name == "h2":
                title = el.get_text(strip=True)
                if title:
                    nodes.append(("h2", title, None))
                continue
            href = el.get("href")
            if not href:
                continue
            url = urljoin(root_url, href)
            if not urlparse(url).path.startswith(top):
                continue  # logo / cross-guide link
            nodes.append(("a", el.get_text(strip=True), url))

        # Guide root = longest common directory prefix of the article paths;
        # article level = path depth below it (0-based).
        art_dirs = [posixpath.dirname(urlparse(u).path) for k, _, u in nodes if k == "a"]
        guide_root = posixpath.commonpath(art_dirs) if art_dirs else top.rstrip("/")

        def article_level(url: str) -> int:
            rel = urlparse(url).path[len(guide_root):].lstrip("/")
            return len(rel.split("/")) - 1

        # Pass 2: emit in DOM order. A section sits one level above the next
        # article it introduces; articles take their URL-derived level.
        out: list[TocEntry] = []
        for i, (kind, title, url) in enumerate(nodes):
            if kind == "a":
                out.append(TocEntry(title=title, url=url, level=article_level(url),
                                    is_article=True))
            else:
                nxt = next((article_level(u) for k, _, u in nodes[i + 1:] if k == "a"), 0)
                out.append(TocEntry(title=title, url=None, level=max(0, nxt - 1),
                                    is_article=False))
        return out
```

- [ ] **Step 4: Register the profile**

In `backend/app/services/profiles/rspress.py`, the `PROFILE = RspressProfile()` line already exists; add the registration call directly after it (replace the trailing comment from Task 1):

```python
PROFILE = RspressProfile()
registry.register(PROFILE)
```

In `backend/app/services/profiles/__init__.py`, add the import alongside the other profile imports, immediately before the `generic` import line (`from app.services.profiles import generic  # noqa: F401,E402`):

```python
from app.services.profiles import rspress  # noqa: F401,E402
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/test_profiles_rspress.py -v`
Expected: PASS (all tests, including Task 1's).

- [ ] **Step 6: Run the full profile + detector suite for regressions**

Run: `pytest tests/test_profiles_prerendered_toc.py tests/test_profiles_endpoint.py tests/test_static_platform_profiles.py tests/test_profile_interface.py -q`
Expected: PASS (registering a new profile must not disturb existing detection/listing).

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/profiles/rspress.py backend/app/services/profiles/__init__.py backend/tests/test_profiles_rspress.py
git commit -m "feat(profiles): rspress build_toc — parse sidebar into ordered nested TOC"
```

---

### Task 3: Live validation on AvePoint Learn

Deploy and re-extract the existing AvePoint M365 source to confirm end-to-end behaviour. No code; this is the rollout/verification gate from the spec. (Run by the controller, not a subagent — it needs cluster + DB access.)

**Files:** none.

- [ ] **Step 1: Confirm the full backend suite is green**

Run: `cd backend && pytest -q`
Expected: PASS (previous total + the new rspress tests).

- [ ] **Step 2: Merge + deploy**

After merge and CI build the `sha-<gitsha>` images, deploy via the k8s runbook (`helm -n docextractor get values docextractor -o yaml > vals.yaml`; bump `image.backend.tag`/`image.frontend.tag` to the new `sha-`; `helm upgrade docextractor deploy/helm/docextractor -f vals.yaml -n docextractor`); wait for backend + worker rollout.

- [ ] **Step 3: Re-extract the AvePoint M365 source**

Port-forward the backend (`kubectl -n docextractor port-forward svc/docextractor-backend 58000:8000`) and trigger:
`curl -s -X POST http://localhost:58000/api/extraction/trigger/5b43d03a-75bf-42d4-8d21-e6048673a87c`
(The source's `platform`/`profile_config` are already NULL, so it auto-detects `rspress`.)

- [ ] **Step 4: Verify the result**

Port-forward Postgres and check (DB creds from the `docextractor-secret` `DOCEXTRACTOR_DATABASE_URL_SYNC`):
- The run COMPLETED; `documentation_sources.platform = 'rspress'` for `5b43d03a-...`.
- `SELECT count(*) FROM toc_entries WHERE source_id='5b43d03a-...' AND url IS NOT NULL;` is ≈109 (the scrapable pages), and there are also structural rows with `url IS NULL` (sections).
- `SELECT count(*) FROM articles WHERE source_id='5b43d03a-...';` is ≈109 with non-empty `content_markdown`.
- Spot-check one article's `content_markdown` contains real body text and **no** sidebar/nav/footer bleed (e.g. no "On this page", no "Edit this page").
- Spot-check nesting: a level-2 page (e.g. a `.../app-profile-authentication/...` permission page) has a non-NULL `parent_id` chain back to its section.

Expected: all checks pass. If TOC count is far below ~109 or content bleeds chrome, return to Task 2 (the parser) rather than patching live data.

---

## Notes for the implementer

- Run everything from `backend/`. The repo also has a `frontend/`; this plan touches neither it nor any backend module besides the two profile files.
- `_resolve_toc_parents` lives in `backend/app/services/firecrawl.py` and is imported directly in tests; importing `firecrawl` pulls in settings but no live connections, matching how other profile tests import it.
- The `posixpath.commonpath` call requires a non-empty list — the `if art_dirs else` guard handles the no-article case.
