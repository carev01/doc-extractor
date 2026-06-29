# Oxygen XML WebHelp Profile Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract login-walled, WAF-protected Oxygen XML WebHelp docs (Rubrik) via a new `oxygen_webhelp` profile: complete page set from the search-index inventory, paced authenticated raw-HTTP content, faithful TOC hierarchy rebuilt from per-page fragments captured during the scrape, and pause+webhook-notify on session expiry.

**Architecture:** A `raw_http` profile builds a flat TOC from `htmlFileInfoList.js`; the raw-HTTP content path is paced (low concurrency, request delay, 401-backoff) and captures each page's `#wh_publication_toc` fragment to a new `articles.toc_fragment` column; after content completes, a post-process stitches the fragments into the authored tree and rewrites the TOC. On mid-run session expiry the run auto-pauses (checkpoint kept), the realm is marked EXPIRED, and a webhook fires.

**Tech Stack:** Python 3, FastAPI, SQLAlchemy (async), Alembic, BeautifulSoup4, httpx, pytest. Run backend commands from `backend/` with `python3`/`pytest`.

## Global Constraints

- Profile module `backend/app/services/profiles/oxygen_webhelp.py`; class `OxygenWebhelpProfile`; `name="oxygen_webhelp"`; `content_engine="raw_http"`; registered in `profiles/__init__.py` before `generic`.
- `detect` true only when both `"oxygen-webhelp"` and `"wh_publication_toc"` are in the root HTML.
- Inventory file path: `<pub_root>/oxygen-webhelp/app/search/index/htmlFileInfoList.js`, format `var htmlFileInfoList = ["relpath.html@@@Title@@@Desc", …]`; `pub_root` = the URL substring up to and including the path segment before `oxygen-webhelp/`.
- `content_config`: `includeTags=["article"]`, `excludeTags=[".related-links","nav","header","footer",".wh_breadcrumb"]`, `onlyMainContent=False`.
- Pacing (profile class attributes, read by the raw-HTTP path): `raw_http_concurrency=2`, `raw_http_request_delay=0.3`, `raw_http_retry_statuses=(401,429,502,503,504)`.
- Fragment capture selector: `toc_fragment_selector="#wh_publication_toc"`. Captured outerHTML stored in nullable `articles.toc_fragment`.
- On mid-run expiry: **pause** (`RunStatus.PAUSED`, keep checkpoint via `RunControlSignal("pause")`) + `realm_manager.invalidate(EXPIRED)` + `notify(...)` — never fail for auth expiry. Non-auth failures still fail loudly (`RawContentScrapeError`).
- Notification: `notify_webhook_url` setting (`DOCEXTRACTOR_NOTIFY_WEBHOOK_URL`, blank=off); best-effort JSON POST; never blocks the run.
- TocEntry order is DFS pre-order; `parent_url` set where known so `_resolve_toc_parents` nests by it. Unauthenticated and non-oxygen behavior must be unchanged.

---

### Task 1: `articles.toc_fragment` column + migration

**Files:**
- Modify: `backend/app/models/article.py` (add column)
- Create: `backend/alembic/versions/<rev>_article_toc_fragment.py`
- Test: `backend/tests/test_article_toc_fragment.py`

**Interfaces:**
- Produces: `Article.toc_fragment: Mapped[str | None]` (nullable Text).

- [ ] **Step 1: Write the failing test**

```python
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.models.article import Article

def test_article_has_toc_fragment_column():
    col = Article.__table__.columns.get("toc_fragment")
    assert col is not None
    assert col.nullable is True
```

- [ ] **Step 2: Run it — FAIL** (`pytest tests/test_article_toc_fragment.py -v`) — column missing.

- [ ] **Step 3: Add the column** in `backend/app/models/article.py` near `content_html`:

```python
    content_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Captured TOC-tree fragment (e.g. Oxygen WebHelp #wh_publication_toc outerHTML)
    # used to post-process the authored hierarchy without re-fetching. Nullable;
    # populated only by profiles that set ``toc_fragment_selector``.
    toc_fragment: Mapped[str | None] = mapped_column(Text, nullable=True)
```

(`Text` is already imported in this module — `content_markdown` uses it.)

- [ ] **Step 4: Create the migration.** Inspect the latest revision: `ls backend/alembic/versions` and read its `down_revision`/`revision`. Create `backend/alembic/versions/<rev>_article_toc_fragment.py` (use a new hex `revision`, set `down_revision` to the current head):

```python
"""add articles.toc_fragment

Revision ID: a1b2c3d4e5f6
Revises: <CURRENT_HEAD_REVISION>
Create Date: 2026-06-29
"""
from alembic import op
import sqlalchemy as sa

revision = "a1b2c3d4e5f6"
down_revision = "<CURRENT_HEAD_REVISION>"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.add_column("articles", sa.Column("toc_fragment", sa.Text(), nullable=True))

def downgrade() -> None:
    op.drop_column("articles", "toc_fragment")
```

Replace `<CURRENT_HEAD_REVISION>` with the actual current head (the `revision` of the file `alembic heads`/the latest versions file reports). Keep the `revision` id unique.

- [ ] **Step 5: Run it — PASS** (`pytest tests/test_article_toc_fragment.py -v`). Also `alembic upgrade head` against the test DB if available, else rely on `create_all` (the model test suffices for CI).

- [ ] **Step 6: Commit**

```bash
git add backend/app/models/article.py backend/alembic/versions/ backend/tests/test_article_toc_fragment.py
git commit -m "feat(model): add nullable articles.toc_fragment column"
```

---

### Task 2: `oxygen_webhelp` profile — detect, build_toc, content_config, attrs

**Files:**
- Create: `backend/app/services/profiles/oxygen_webhelp.py`
- Modify: `backend/app/services/profiles/__init__.py`
- Test: `backend/tests/test_profiles_oxygen_webhelp.py`

**Interfaces:**
- Consumes: `app.services.profiles.base.TocEntry`, `registry`, `app.services.profiles.scraper.FakeScraper`, `app.services.profiles.content_scope.scope_content_html`, `app.services.profiles.detector.detect_platform`.
- Produces: `OxygenWebhelpProfile` with `name`, `content_engine="raw_http"`, `detect`, async `build_toc`, `content_config`, and class attrs `raw_http_concurrency=2`, `raw_http_request_delay=0.3`, `raw_http_retry_statuses=(401,429,502,503,504)`, `toc_fragment_selector="#wh_publication_toc"`. Module `PROFILE = OxygenWebhelpProfile()` registered. (`rebuild_toc` lands in Task 5.)

- [ ] **Step 1: Write the failing tests**

```python
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pytest
from app.services.profiles.scraper import FakeScraper
from app.services.profiles.detector import detect_platform
from app.services.profiles.content_scope import scope_content_html
from app.services.profiles.oxygen_webhelp import OxygenWebhelpProfile

ROOT = "https://docs.example.com/en-us/saas/saas/common/getting_started.html"
INVENTORY_URL = "https://docs.example.com/en-us/saas/oxygen-webhelp/app/search/index/htmlFileInfoList.js"

PAGE = """
<html><body>
  <script src="../../oxygen-webhelp/app/commons.js"></script>
  <nav id="wh_publication_toc"><ul><li role="treeitem" data-tocid="t1"><a href="../../saas/common/getting_started.html">Getting Started</a></li></ul></nav>
  <article><h1>Getting Started</h1><p>Body text here.</p></article>
  <div class="wh_breadcrumb">Home</div>
  <footer>footer</footer>
</body></html>
"""
INVENTORY = 'var htmlFileInfoList = ["common/intro.html@@@Intro@@@d", "OLVM/add.html@@@Add OLVM@@@d", "OLVM/edit.html@@@Edit OLVM@@@d"];'

def _scraper():
    return FakeScraper({}, raw_by_url={ROOT: PAGE, INVENTORY_URL: INVENTORY})

def test_opts_into_raw_http_and_attrs():
    p = OxygenWebhelpProfile()
    assert p.content_engine == "raw_http"
    assert p.raw_http_concurrency == 2
    assert p.raw_http_request_delay == 0.3
    assert 401 in p.raw_http_retry_statuses
    assert p.toc_fragment_selector == "#wh_publication_toc"

def test_detect_needs_both_hooks():
    p = OxygenWebhelpProfile()
    assert p.detect(PAGE, ROOT) is True
    assert p.detect('<div class="oxygen-webhelp"></div>', ROOT) is False
    assert p.detect('<nav id="wh_publication_toc"></nav>', ROOT) is False

def test_detects_via_registry():
    assert detect_platform(PAGE, ROOT) == "oxygen_webhelp"

@pytest.mark.asyncio
async def test_build_toc_from_inventory():
    toc = await OxygenWebhelpProfile().build_toc(ROOT, _scraper())
    # build_toc levels are placeholders (= path.count("/")); the authored
    # hierarchy is set later by the rebuild. What matters here: complete entries
    # with correct titles + absolute URLs resolved against pub_root.
    shape = [(e.level, e.title, e.url) for e in toc]
    assert shape == [
        (1, "Intro", "https://docs.example.com/en-us/saas/common/intro.html"),
        (1, "Add OLVM", "https://docs.example.com/en-us/saas/OLVM/add.html"),
        (1, "Edit OLVM", "https://docs.example.com/en-us/saas/OLVM/edit.html"),
    ]
    assert all(e.is_article for e in toc)

@pytest.mark.asyncio
async def test_build_toc_empty_when_no_oxygen_ref():
    s = FakeScraper({}, raw_by_url={ROOT: "<html><body><article>x</article></body></html>"})
    assert await OxygenWebhelpProfile().build_toc(ROOT, s) == []

def test_content_config_scopes_article():
    cfg = OxygenWebhelpProfile().content_config()
    out = scope_content_html(PAGE, ROOT, cfg["includeTags"], cfg["excludeTags"])
    assert "Body text here." in out
    assert "Getting Started" in out
    assert "Home" not in out         # breadcrumb dropped
    assert "footer" not in out
```

- [ ] **Step 2: Run — FAIL** (`pytest tests/test_profiles_oxygen_webhelp.py -v`) — module missing.

- [ ] **Step 3: Create the profile** `backend/app/services/profiles/oxygen_webhelp.py`:

```python
"""Oxygen XML WebHelp profile (e.g. Rubrik docs.rubrik.com).

Login-walled + WAF-protected. Content is server-rendered in-page (``article``);
the full TOC is not in any single page (the on-page #wh_publication_toc is
contextual) and there is no TOC file — but Oxygen's search index ships a
complete page inventory at
``<pub_root>/oxygen-webhelp/app/search/index/htmlFileInfoList.js``. build_toc
uses that for a complete (flat, URL-ordered) TOC; the authored hierarchy is
rebuilt post-scrape from per-page #wh_publication_toc fragments (see rebuild_toc,
added later). Runs on the authenticated raw-HTTP path, paced for the WAF.
"""

import json
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from app.services.profiles import registry
from app.services.profiles.base import TocEntry

_INVENTORY_REL = "oxygen-webhelp/app/search/index/htmlFileInfoList.js"
_OXYGEN_REF = re.compile(r'["\'](?:[^"\']*?/)?oxygen-webhelp/')


def _pub_root(page_url: str, html: str) -> str | None:
    """The publication root: the absolute URL up to (and including) the segment
    before ``oxygen-webhelp/``, derived from an oxygen-webhelp asset ref."""
    m = _OXYGEN_REF.search(html)
    if not m:
        return None
    ref = m.group(0).strip('"\'')
    abs_ref = urljoin(page_url, ref)            # .../en-us/saas/oxygen-webhelp/
    return abs_ref.split("oxygen-webhelp/")[0]  # .../en-us/saas/


class OxygenWebhelpProfile:
    name = "oxygen_webhelp"
    content_engine = "raw_http"
    # WAF pacing (read by the raw-HTTP content path).
    raw_http_concurrency = 2
    raw_http_request_delay = 0.3
    raw_http_retry_statuses = (401, 429, 502, 503, 504)
    # Capture each page's TOC tree fragment for the post-process hierarchy rebuild.
    toc_fragment_selector = "#wh_publication_toc"

    def detect(self, root_html: str, root_url: str) -> bool:
        return "oxygen-webhelp" in root_html and "wh_publication_toc" in root_html

    async def build_toc(self, root_url: str, scraper) -> list[TocEntry]:
        try:
            html = await scraper.get_raw(root_url)
        except Exception:
            return []
        pub_root = _pub_root(root_url, html or "")
        if not pub_root:
            return []
        try:
            raw = await scraper.get_raw(pub_root + _INVENTORY_REL)
        except Exception:
            return []
        m = re.search(r"htmlFileInfoList\s*=\s*(\[.*\])", raw or "", re.S)
        if not m:
            return []
        try:
            entries = json.loads(m.group(1))
        except Exception:
            return []
        out: list[TocEntry] = []
        seen: set[str] = set()
        for s in entries:
            parts = s.split("@@@")
            path = parts[0].strip()
            if not path or path in seen:
                continue
            seen.add(path)
            title = parts[1].strip() if len(parts) > 1 and parts[1].strip() else path
            out.append(TocEntry(
                title=title, url=urljoin(pub_root, path),
                level=path.count("/"), is_article=True,
            ))
        return out

    def content_config(self) -> dict:
        return {
            "includeTags": ["article"],
            "excludeTags": [".related-links", "nav", "header", "footer", ".wh_breadcrumb"],
            "onlyMainContent": False,
        }


PROFILE = OxygenWebhelpProfile()
registry.register(PROFILE)
```

- [ ] **Step 4: Register** — in `backend/app/services/profiles/__init__.py`, add before the `generic` import:

```python
from app.services.profiles import oxygen_webhelp  # noqa: F401,E402
```

- [ ] **Step 5: Run — PASS** (`pytest tests/test_profiles_oxygen_webhelp.py -v`), then regression `pytest tests/test_profile_interface.py tests/test_static_platform_profiles.py -q`.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/profiles/oxygen_webhelp.py backend/app/services/profiles/__init__.py backend/tests/test_profiles_oxygen_webhelp.py
git commit -m "feat(profiles): oxygen_webhelp — detect + inventory build_toc + content_config"
```

---

### Task 3: Raw-HTTP pacing — fetch_raw retry_statuses + per-profile concurrency/delay

**Files:**
- Modify: `backend/app/services/firecrawl.py` (`fetch_raw` signature; `_request_with_retry` retry set; `_scrape_via_raw_http` reads profile pacing)
- Test: `backend/tests/test_raw_http_pacing.py`

**Interfaces:**
- Consumes: profile attrs `raw_http_concurrency`, `raw_http_request_delay`, `raw_http_retry_statuses`.
- Produces: `fetch_raw(self, url, cookies=None, retry_statuses=None)`.

- [ ] **Step 1: Write the failing test**

```python
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import httpx, pytest
from app.services.firecrawl import FirecrawlService

@pytest.mark.asyncio
async def test_fetch_raw_retries_401_when_in_retry_statuses():
    calls = {"n": 0}
    def handler(req):
        calls["n"] += 1
        if calls["n"] < 2:
            return httpx.Response(401, text="nope")
        return httpx.Response(200, text="<html>ok</html>")
    svc = FirecrawlService()
    svc.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    # shrink backoff for the test if the service exposes it; otherwise this is fast enough
    out = await svc.fetch_raw("https://x/p", retry_statuses={401})
    assert "ok" in out and calls["n"] == 2
    await svc.client.aclose()

@pytest.mark.asyncio
async def test_fetch_raw_does_not_retry_401_by_default():
    calls = {"n": 0}
    def handler(req):
        calls["n"] += 1
        return httpx.Response(401, text="nope")
    svc = FirecrawlService()
    svc.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(Exception):
        await svc.fetch_raw("https://x/p")
    assert calls["n"] == 1   # 401 not retried by default
    await svc.client.aclose()
```

- [ ] **Step 2: Run — FAIL** (`fetch_raw` has no `retry_statuses`; 401 currently raises immediately without the param).

- [ ] **Step 3: Implement.** In `fetch_raw`, accept `retry_statuses` and pass it to the retry wrapper:

```python
    async def fetch_raw(self, url: str, cookies: list[dict] | None = None,
                        retry_statuses: "set[int] | tuple[int, ...] | None" = None) -> str:
        headers = {"User-Agent": _BROWSER_UA}
        ck = _cookie_header(cookies)
        if ck:
            headers["Cookie"] = ck
        resp = await self._request_with_retry(
            lambda: self.client.get(url, headers=headers, follow_redirects=True),
            what=f"raw GET {url}",
            retry_statuses=retry_statuses,
        )
        return resp.text
```

In `_request_with_retry`, add the param and include it in the retry decision (find the existing `if code not in self.TRANSIENT_STATUS ...` guard and union the extra statuses):

```python
    async def _request_with_retry(self, send, what, retry_statuses=None):
        retryable = set(self.TRANSIENT_STATUS) | set(retry_statuses or ())
        ...
        # in the HTTPStatusError branch, replace `self.TRANSIENT_STATUS` membership with `retryable`:
        if code not in retryable or attempt >= self.TRANSIENT_RETRIES:
            raise BrowserlessError(...) / re-raise as today
```

(Keep the existing default behavior when `retry_statuses` is None — `retryable` == `TRANSIENT_STATUS`.)

In `_scrape_via_raw_http`, read pacing from the profile: replace the chunk-size line and add a per-request delay + pass `retry_statuses` to the page fetch:

```python
        chunk_size = max(1, getattr(profile, "raw_http_concurrency", settings.raw_http_concurrency))
        request_delay = float(getattr(profile, "raw_http_request_delay", 0) or 0)
        retry_statuses = getattr(profile, "raw_http_retry_statuses", None)
```

and in the inner `_fetch`:

```python
        async def _fetch(url: str) -> str | None:
            try:
                if request_delay:
                    await asyncio.sleep(request_delay)
                return await self.fetch_raw(url, cookies=auth_cookies, retry_statuses=retry_statuses)
```

(`asyncio` is already imported in firecrawl.py.)

- [ ] **Step 4: Run — PASS** (`pytest tests/test_raw_http_pacing.py -v`).

- [ ] **Step 5: Regression** `pytest tests/test_fetch_raw_cookies.py tests/test_content_path_selection.py -q`.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/firecrawl.py backend/tests/test_raw_http_pacing.py
git commit -m "feat(extract): per-profile raw_http pacing + retry_statuses (401 backoff)"
```

---

### Task 4: Raw-HTTP fragment capture → `articles.toc_fragment`

**Files:**
- Modify: `backend/app/services/firecrawl.py` (`_scrape_via_raw_http` capture; `process_article_result` accepts `toc_fragment`)
- Test: `backend/tests/test_toc_fragment_capture.py`

**Interfaces:**
- Consumes: `Article.toc_fragment` (Task 1); `profile.toc_fragment_selector` (Task 2).
- Produces: `process_article_result(..., toc_fragment: str | None = None)` persists the fragment; `_scrape_via_raw_http` extracts the selector's outerHTML from the full page HTML and passes it.

- [ ] **Step 1: Write the failing test** (focused on the helper that extracts + the persistence path). Add a unit test for a small extractor function `_extract_fragment(html, selector) -> str | None` you will introduce, plus that `process_article_result` writes `toc_fragment`:

```python
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.services.firecrawl import _extract_fragment

def test_extract_fragment_returns_outer_html():
    html = '<html><body><nav id="wh_publication_toc"><ul><li>x</li></ul></nav><article>a</article></body></html>'
    frag = _extract_fragment(html, "#wh_publication_toc")
    assert frag is not None
    assert 'id="wh_publication_toc"' in frag and "<li>x</li>" in frag
    assert "<article>" not in frag

def test_extract_fragment_none_when_absent():
    assert _extract_fragment("<html><body><p>x</p></body></html>", "#wh_publication_toc") is None
```

(Persisting through `process_article_result` is covered end-to-end by the live validation; the unit test pins the extractor.)

- [ ] **Step 2: Run — FAIL** (`_extract_fragment` undefined).

- [ ] **Step 3: Implement.** Add the helper near the other module helpers in `firecrawl.py`:

```python
def _extract_fragment(html: str, selector: str) -> str | None:
    """Return the outer HTML of the first ``selector`` match, or None."""
    from bs4 import BeautifulSoup
    el = BeautifulSoup(html or "", "html.parser").select_one(selector)
    return str(el) if el is not None else None
```

Add a `toc_fragment` parameter to `process_article_result` (default None) and set it on the article in both the create and update branches (alongside `content_html`):

```python
    async def process_article_result(self, ..., toc_fragment: str | None = None):
        ...
        # in the create path:
        article = Article(..., content_html=doc_html, toc_fragment=toc_fragment, ...)
        # in the update path (existing_article):
        existing_article.content_html = doc_html
        if toc_fragment is not None:
            existing_article.toc_fragment = toc_fragment
```

In `_scrape_via_raw_http`, when the profile sets `toc_fragment_selector`, extract the fragment from the **full page HTML** (the `html` variable before `extract_body`) and pass it to `process_article_result`:

```python
        frag_selector = getattr(profile, "toc_fragment_selector", None)
        ...
        # where the per-page result is processed (after fetching `html`, before/with extract_body):
        toc_fragment = _extract_fragment(html, frag_selector) if frag_selector else None
        await self.process_article_result(..., toc_fragment=toc_fragment)
```

(Locate the existing `process_article_result(...)` call inside `_scrape_via_raw_http` and add the `toc_fragment=toc_fragment` kwarg; compute `toc_fragment` from the same `html` that `extract_body` is called on.)

- [ ] **Step 4: Run — PASS** (`pytest tests/test_toc_fragment_capture.py -v`).

- [ ] **Step 5: Regression** `pytest tests/test_extract_auth_realm.py -q` (article processing path unaffected for other profiles — `toc_fragment` defaults None).

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/firecrawl.py backend/tests/test_toc_fragment_capture.py
git commit -m "feat(extract): capture per-page TOC fragment to articles.toc_fragment"
```

---

### Task 5: `oxygen_webhelp.rebuild_toc` — stitch fragments into the authored tree

**Files:**
- Modify: `backend/app/services/profiles/oxygen_webhelp.py` (add `rebuild_toc`)
- Test: `backend/tests/test_profiles_oxygen_webhelp.py` (append)

**Interfaces:**
- Produces: `OxygenWebhelpProfile.rebuild_toc(fragments: list[tuple[str, str]], root_url: str) -> list[TocEntry]` — `fragments` is `(page_url, fragment_html)`; returns DFS pre-order entries with `level` + `parent_url`.

- [ ] **Step 1: Write the failing test.** Two overlapping contextual fragments describing a 3-level tree (root → section → leaf), each with the current page's branch expanded:

```python
FRAG_SECTION = """
<nav id="wh_publication_toc"><ul>
  <li role="treeitem" data-tocid="root"><div class="topicref"><a href="../root.html">Root</a></div>
    <ul>
      <li role="treeitem" data-tocid="secA"><div class="topicref"><a href="../a/secA.html">Section A</a></div>
        <ul><li role="treeitem" data-tocid="leaf1"><div class="topicref"><a href="../a/leaf1.html">Leaf 1</a></div></li></ul>
      </li>
      <li role="treeitem" data-tocid="secB"><div class="topicref"><a href="../b/secB.html">Section B</a></div></li>
    </ul>
  </li>
</ul></nav>
"""
FRAG_LEAF2 = """
<nav id="wh_publication_toc"><ul>
  <li role="treeitem" data-tocid="root"><div class="topicref"><a href="../root.html">Root</a></div>
    <ul>
      <li role="treeitem" data-tocid="secA"><div class="topicref"><a href="../a/secA.html">Section A</a></div></li>
      <li role="treeitem" data-tocid="secB"><div class="topicref"><a href="../b/secB.html">Section B</a></div>
        <ul><li role="treeitem" data-tocid="leaf2"><div class="topicref"><a href="../b/leaf2.html">Leaf 2</a></div></li></ul>
      </li>
    </ul>
  </li>
</ul></nav>
"""

def test_rebuild_toc_stitches_full_tree():
    base = "https://d.example.com/en-us/saas/saas/"
    frags = [(base + "a/leaf1.html", FRAG_SECTION), (base + "b/leaf2.html", FRAG_LEAF2)]
    toc = OxygenWebhelpProfile().rebuild_toc(frags, base + "common/start.html")
    shape = [(e.level, e.title) for e in toc]
    assert shape == [
        (0, "Root"),
        (1, "Section A"),
        (2, "Leaf 1"),
        (1, "Section B"),
        (2, "Leaf 2"),
    ]
    # parent_url linkage
    by_title = {e.title: e for e in toc}
    assert by_title["Leaf 1"].parent_url == by_title["Section A"].url
    assert by_title["Section B"].parent_url == by_title["Root"].url

def test_rebuild_toc_empty_when_no_fragments():
    assert OxygenWebhelpProfile().rebuild_toc([], "https://d/x.html") == []
```

- [ ] **Step 2: Run — FAIL** (`rebuild_toc` undefined).

- [ ] **Step 3: Implement `rebuild_toc`** in `oxygen_webhelp.py`:

```python
    def rebuild_toc(self, fragments: "list[tuple[str, str]]", root_url: str) -> list[TocEntry]:
        node: dict[str, dict] = {}          # tocid -> {url, title}
        parent_of: dict[str, str] = {}      # tocid -> parent tocid
        children: dict[str, list[str]] = {} # tocid -> ordered child tocids (longest seen)
        top_order: list[str] = []           # ordered top-level tocids (longest seen)

        def tocid(li):
            if li.get("data-tocid"):
                return li["data-tocid"]
            d = li.find(attrs={"data-tocid": True})
            return d["data-tocid"] if d else None

        def direct_child_items(container):
            # treeitem <li> that are this container's nearest treeitem descendants
            out = []
            for ul in container.find_all("ul", recursive=False):
                out.extend([li for li in ul.find_all("li", recursive=False)
                            if li.get("role") == "treeitem"])
            return out

        for page_url, frag in fragments:
            soup = BeautifulSoup(frag or "", "html.parser")
            navs = soup.select("#wh_publication_toc") or [soup]
            nav = navs[0]
            # top-level
            tops = [t for li in direct_child_items(nav) for t in [tocid(li)] if t]
            if len(tops) > len(top_order):
                top_order = tops
            for li in nav.find_all("li", attrs={"role": "treeitem"}):
                tid = tocid(li)
                if not tid:
                    continue
                a = li.find("a", href=True)
                if a is not None and tid not in node:
                    node[tid] = {"url": urljoin(page_url, a["href"]),
                                 "title": a.get_text(strip=True) or a["href"]}
                pli = li.find_parent("li", attrs={"role": "treeitem"})
                ptid = tocid(pli) if pli is not None else None
                if ptid and tid not in parent_of:
                    parent_of[tid] = ptid
                kids = [t for c in direct_child_items(li) for t in [tocid(c)] if t]
                if len(kids) > len(children.get(tid, [])):
                    children[tid] = kids

        if not node:
            return []
        out: list[TocEntry] = []
        seen: set[str] = set()

        def walk(tid: str, level: int, parent_url):
            if tid in seen or tid not in node:
                return
            seen.add(tid)
            n = node[tid]
            out.append(TocEntry(title=n["title"], url=n["url"], level=level,
                                is_article=True, parent_url=parent_url))
            for c in children.get(tid, []):
                walk(c, level + 1, n["url"])

        roots = top_order or [t for t in node if t not in parent_of]
        for t in roots:
            walk(t, 0, None)
        for t in node:               # any unreached nodes → append at top
            if t not in seen:
                walk(t, 0, None)
        return out
```

(`urljoin` is already imported in the module.)

- [ ] **Step 4: Run — PASS** (`pytest tests/test_profiles_oxygen_webhelp.py -v`).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/profiles/oxygen_webhelp.py backend/tests/test_profiles_oxygen_webhelp.py
git commit -m "feat(profiles): oxygen_webhelp.rebuild_toc — stitch fragments into authored tree"
```

---

### Task 6: Post-process TOC rebuild wiring (extract_source)

**Files:**
- Modify: `backend/app/services/firecrawl.py` (after the raw-HTTP content scrape: rebuild + re-persist TOC + re-link articles)
- Test: `backend/tests/test_toc_rebuild_persist.py`

**Interfaces:**
- Consumes: `profile.rebuild_toc` (Task 5), `Article.toc_fragment` (Task 1), `_resolve_toc_parents`.
- Produces: a helper `_persist_toc(db, source_id, toc_entries) -> dict[str,uuid]` extracted from the existing phase-1 persistence loop, reused for both phase-1 and the rebuild.

- [ ] **Step 1: Write the failing test** for the extracted persistence helper (pure-ish; uses a sync test DB session like other `tests/`):

```python
# Verifies _persist_toc deletes old entries, sets parent_id by level/parent_url,
# and returns url->id. Mirrors the style of tests/test_reconcile_removals.py
# (sync psycopg2 Session). See that file for the session fixture pattern.
```

Model the DB fixture on `backend/tests/test_reconcile_removals.py`. Assert that after `_persist_toc(db, source_id, [TocEntry(...3-level tree...)])`, the `toc_entries` rows have correct `level`/`parent_id` and the returned map has all urls.

- [ ] **Step 2: Run — FAIL** (`_persist_toc` undefined).

- [ ] **Step 3: Extract `_persist_toc`.** Refactor the existing phase-1 TOC persistence (the `delete(TOCEntry)...` + `_resolve_toc_parents` + insert loop) into a method `async def _persist_toc(self, db, source_id, toc_entries: list[dict]) -> dict[str, uuid.UUID]` that returns the url→id map, and call it from phase 1 (no behavior change there). `toc_entries` items are dicts with `title,url,level,is_article,parent_url,sort_order` (build the dicts from `TocEntry` the same way phase 1 does).

- [ ] **Step 4: Add the rebuild call** after `_scrape_via_raw_http` returns successfully (the `path == "raw_http"` branch), guarded by the profile having `rebuild_toc`:

```python
            if path == "raw_http" and getattr(profile, "rebuild_toc", None):
                rows = (await db.execute(
                    select(Article.source_url, Article.toc_fragment)
                    .where(Article.source_id == source_id,
                           Article.removed_at.is_(None),
                           Article.toc_fragment.is_not(None))
                )).all()
                fragments = [(u, f) for (u, f) in rows if f]
                if fragments:
                    rebuilt = profile.rebuild_toc(fragments, source.base_url)
                    if rebuilt:
                        toc_dicts = [{
                            "title": e.title, "url": e.url, "level": e.level,
                            "is_article": e.is_article, "parent_url": e.parent_url,
                            "sort_order": i,
                            "topic_key": derive_topic_key(e.url, source.url_template, product_version),
                        } for i, e in enumerate(rebuilt)]
                        url_to_id = await self._persist_toc(db, source_id, toc_dicts)
                        # re-link existing articles to the rebuilt TOC entries
                        for url, tid in url_to_id.items():
                            await db.execute(
                                update(Article).where(
                                    Article.source_id == source_id,
                                    Article.source_url == url,
                                ).values(toc_entry_id=tid)
                            )
                        await db.commit()
                        logger.info("Rebuilt TOC hierarchy for %s: %d entries", source_id, len(rebuilt))
                    else:
                        logger.warning("rebuild_toc produced no entries for %s — keeping inventory TOC", source_id)
```

(`derive_topic_key`, `product_version`, `update`, `select`, `Article` are already in scope in `extract_source`; confirm and import if needed.)

- [ ] **Step 5: Run — PASS** (`pytest tests/test_toc_rebuild_persist.py -v`), then regression `pytest tests/test_reconcile_removals.py tests/test_extract_auth_realm.py -q`.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/firecrawl.py backend/tests/test_toc_rebuild_persist.py
git commit -m "feat(extract): post-process TOC rebuild from captured fragments (raw_http)"
```

---

### Task 7: Webhook notification helper + setting

**Files:**
- Create: `backend/app/services/notify.py`
- Modify: `backend/app/core/config.py` (`notify_webhook_url`); `deploy/helm/docextractor/templates/secret.yaml` + `values.yaml`
- Test: `backend/tests/test_notify.py`

**Interfaces:**
- Produces: `async def notify(title: str, message: str, **fields) -> None` (best-effort).

- [ ] **Step 1: Write the failing test**

```python
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import httpx, pytest
import app.services.notify as notify_mod

@pytest.mark.asyncio
async def test_notify_posts_when_url_set(monkeypatch):
    seen = {}
    def handler(req):
        seen["url"] = str(req.url); seen["body"] = req.content.decode()
        return httpx.Response(200)
    monkeypatch.setattr(notify_mod.settings, "notify_webhook_url", "https://hook.test/x", raising=False)
    monkeypatch.setattr(notify_mod, "_client_factory", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await notify_mod.notify("Session expired", "Realm X expired", realm="X")
    assert seen["url"] == "https://hook.test/x"
    assert "Realm X expired" in seen["body"]

@pytest.mark.asyncio
async def test_notify_noop_when_unset(monkeypatch):
    monkeypatch.setattr(notify_mod.settings, "notify_webhook_url", "", raising=False)
    await notify_mod.notify("t", "m")   # must not raise / not post

@pytest.mark.asyncio
async def test_notify_swallows_errors(monkeypatch):
    def handler(req): raise httpx.ConnectError("down")
    monkeypatch.setattr(notify_mod.settings, "notify_webhook_url", "https://hook.test/x", raising=False)
    monkeypatch.setattr(notify_mod, "_client_factory", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await notify_mod.notify("t", "m")   # must not raise
```

- [ ] **Step 2: Run — FAIL** (module missing).

- [ ] **Step 3: Implement** `backend/app/services/notify.py`:

```python
"""Best-effort outbound notifications (generic webhook). Never raises."""

import logging

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


def _client_factory() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0))


async def notify(title: str, message: str, **fields) -> None:
    url = (getattr(settings, "notify_webhook_url", "") or "").strip()
    if not url:
        return
    payload = {"title": title, "message": message, "text": f"{title}: {message}",
               "content": f"{title}: {message}", **fields}
    try:
        client = _client_factory()
        try:
            await client.post(url, json=payload)
        finally:
            await client.aclose()
    except Exception as exc:  # best-effort: log and move on
        logger.warning("notify webhook failed: %s", exc)
```

Add to `backend/app/core/config.py` (near `secret_key`):

```python
    # Generic outbound webhook for operator alerts (e.g. realm session expired
    # mid-run). Blank disables. POSTs JSON {title,message,text,content,...}.
    notify_webhook_url: str = ""
```

Wire helm: in `deploy/helm/docextractor/templates/secret.yaml` add `DOCEXTRACTOR_NOTIFY_WEBHOOK_URL: {{ .Values.notifyWebhookUrl | default "" | quote }}`; in `values.yaml` add `notifyWebhookUrl: ""  # OVERRIDE (optional)`.

- [ ] **Step 4: Run — PASS** (`pytest tests/test_notify.py -v`).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/notify.py backend/app/core/config.py deploy/helm/docextractor/templates/secret.yaml deploy/helm/docextractor/values.yaml backend/tests/test_notify.py
git commit -m "feat(notify): best-effort generic webhook + notify_webhook_url setting"
```

---

### Task 8: Pause + EXPIRED + notify on mid-run session expiry

**Files:**
- Modify: `backend/app/services/firecrawl.py` (raw-HTTP auth-expiry → pause+invalidate+notify; browserless `NeedsLoginError` → pause+invalidate+notify)
- Test: `backend/tests/test_expiry_pause.py`

**Interfaces:**
- Consumes: `RunControlSignal("pause")`, `realm_manager.invalidate`, `session_expired` (from `app.services.auth.session`), `notify` (Task 7).

- [ ] **Step 1: Write the failing test** for a decision helper you will add, `_is_auth_expiry(failed_statuses: list[int], realm) -> bool` (true when failures are predominantly 401 and the realm session is expired):

```python
import os, sys, types
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from datetime import datetime, timezone
from app.services.firecrawl import _is_auth_expiry

def _realm(exp):
    return types.SimpleNamespace(state_snapshot={"cookies": [{"name":"t","expires":exp}], "origins": []})

def test_auth_expiry_true_for_401s_and_expired_realm():
    past = datetime.now(timezone.utc).timestamp() - 10
    assert _is_auth_expiry([401, 401, 401], _realm(past)) is True

def test_not_auth_expiry_when_realm_live():
    fut = datetime.now(timezone.utc).timestamp() + 10000
    assert _is_auth_expiry([401, 401], _realm(fut)) is False

def test_not_auth_expiry_without_realm():
    assert _is_auth_expiry([401, 401], None) is False

def test_not_auth_expiry_for_non_401_failures():
    past = datetime.now(timezone.utc).timestamp() - 10
    assert _is_auth_expiry([500, 503], _realm(past)) is False
```

- [ ] **Step 2: Run — FAIL** (`_is_auth_expiry` undefined).

- [ ] **Step 3: Implement.** Add the helper to `firecrawl.py`:

```python
def _is_auth_expiry(failed_statuses: list[int], realm) -> bool:
    from app.services.auth.session import session_expired
    if realm is None or not failed_statuses:
        return False
    n401 = sum(1 for s in failed_statuses if s == 401)
    return n401 >= max(1, len(failed_statuses) // 2) and session_expired(realm)
```

In `_scrape_via_raw_http`, track the HTTP status of failed fetches (capture `exc.response.status_code` where available in the `_fetch` except path into a run-scoped `failed_statuses` list). When the failure-rate guard would raise `RawContentScrapeError`, first check: load the source's realm (if any) and if `_is_auth_expiry(failed_statuses, realm)` → `invalidate(db, realm, RealmStatus.EXPIRED, "Session expired during extraction")`, `await notify("Session expired", f"Realm '{realm.name}' expired during extraction of '{source_name}' — the run is PAUSED. Upload a fresh cookie and hit Resume to continue.", realm=realm.name)`, then `raise RunControlSignal("pause")` (instead of `RawContentScrapeError`). Otherwise raise `RawContentScrapeError` as today. (Pass the realm/source_name into `_scrape_via_raw_http`, or load via `source_id`.)

In the browserless `NeedsLoginError` handler (the `except NeedsLoginError` block), it already calls `realm_manager.invalidate(..., EXPIRED, ...)` and sets the run FAILED — change it to **pause**: set `run.status = RunStatus.PAUSED` (keep checkpoint, do not set FAILED), add `await notify("Session expired", f"Realm '{realm.name}' expired during extraction — the run is PAUSED. Upload a fresh cookie and hit Resume.", realm=realm.name)`, and return.

- [ ] **Step 4: Run — PASS** (`pytest tests/test_expiry_pause.py -v`).

- [ ] **Step 5: Regression** `pytest tests/test_extract_auth_realm.py tests/test_trigger_expired_realm.py -q`.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/firecrawl.py backend/tests/test_expiry_pause.py
git commit -m "feat(extract): pause + EXPIRED + notify on mid-run session expiry"
```

---

### Task 9: Live validation (controller-run, post-merge)

No code. Validates Rubrik end-to-end. Run by the controller (cluster + DB + a fresh cookie).

**Files:** none.

- [ ] **Step 1: Full backend suite** `cd backend && pytest -q` — PASS (previous total + new tests).

- [ ] **Step 2: Merge + deploy** (k8s runbook; bump image to new `sha-`; helm upgrade; rollout). Set `notifyWebhookUrl` in the release values (an ntfy/Slack URL) so expiry pings work.

- [ ] **Step 3: Wire Rubrik** — create vendor/product/source for `https://docs.rubrik.com/en-us/saas/saas/common/getting_started_with_rsc.html`; create a realm (`login_domain=docs.rubrik.com`, session-only) and upload a **fresh** Cookie-Editor export; attach the realm to the source.

- [ ] **Step 4: Trigger + monitor** — trigger extraction. Confirm `platform=oxygen_webhelp`, paced raw-HTTP (low concurrency) avoids sustained WAF 401s, content is clean (`article` body, no chrome), and `articles.toc_fragment` is populated. Expect the run to **pause** when the token expires; verify the webhook fired and the realm is `EXPIRED`; upload a fresh cookie and `POST /runs/{id}/resume`; confirm it continues from the checkpoint.

- [ ] **Step 5: Verify hierarchy** — once content completes, confirm the post-process ran: `toc_entries` for the source form a nested tree (multiple levels, `parent_id` chains) matching the on-site TOC on spot-checked branches, and articles are linked to the rebuilt entries. Tune `raw_http_concurrency`/`raw_http_request_delay` if the WAF still throttles.

---

## Notes for the implementer

- Backend from `backend/`. Touch only the files listed per task; don't refactor unrelated code.
- Tasks 1→2→3→4→5→6 have dependencies (column → profile → pacing → capture → rebuild_toc → wiring); 7 and 8 depend on earlier tasks (notify before pause-wiring). Keep the order.
- The build_toc `level` and the rebuild output both feed `_resolve_toc_parents`; the rebuild (Task 6) overwrites the inventory TOC's flat levels with the stitched hierarchy.
- For any "locate the existing call" step, read the current `_scrape_via_raw_http` / `extract_source` first; only add the described kwargs/branches, leaving surrounding logic intact.
