# Authenticated raw-HTTP + Fern Profile Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let login-walled docs with a gated root (EON/Fern) extract via a fast authenticated raw-HTTP path, add a `fern` profile, and migrate AvePoint (rspress) auth sources onto the same fast path.

**Architecture:** Inject the realm's session cookies into `fetch_raw`; make the discovery-phase `Scraper` auth-aware; route authenticated `raw_http` profiles through the cookie-injecting raw path (instead of forcing Browserless). Add a `fern` raw_http profile that parses the server-rendered Fern sidebar.

**Tech Stack:** Python 3, httpx, BeautifulSoup4, pytest (+ pytest-asyncio). Run all commands from `backend/`.

## Global Constraints

- All new/changed backend code lives under `backend/app/services/`. Run tests from `backend/` with `pytest`; use `python3`.
- Cookie list shape (from realm `state_snapshot["cookies"]`) is a list of dicts each with at least `"name"` and `"value"`.
- `fetch_raw` with no cookies, and every unauthenticated code path, must behave byte-for-byte as before.
- Content-path selection rule: a `raw_http` profile always uses the raw-HTTP path (authenticated or not); an authenticated non-raw_http profile uses Browserless; otherwise Browserless only if `render_engine == "browserless"`, else the Firecrawl batch path.
- `fern` profile: `name="fern"`, `content_engine="raw_http"`; `detect` requires BOTH `"fern-sidebar"` and `"fern-prose"` in the root HTML; sidebar selector `aside.fern-sidebar-desktop`; content `includeTags=[".fern-prose"]`, `excludeTags=[".toc-root","nav","footer"]`, `onlyMainContent=False`.
- Tests are hermetic (no network): mock httpx transport for `fetch_raw`; use `FakeScraper`/inline HTML for the profile (mirror `backend/tests/test_profiles_prerendered_toc.py` and `test_profiles_rspress.py`).
- Do not change schema, migrations, or config.

---

### Task 1: Cookie-injecting `fetch_raw`

**Files:**
- Modify: `backend/app/services/firecrawl.py` (`fetch_raw`, add `_cookie_header` helper)
- Test: `backend/tests/test_fetch_raw_cookies.py` (new)

**Interfaces:**
- Produces: module function `_cookie_header(cookies: list[dict] | None) -> str | None`; `FirecrawlService.fetch_raw(self, url: str, cookies: list[dict] | None = None) -> str`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_fetch_raw_cookies.py`:

```python
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx
import pytest

from app.services.firecrawl import FirecrawlService, _cookie_header


def test_cookie_header_builds_pairs():
    assert _cookie_header([{"name": "a", "value": "1"}, {"name": "b", "value": "2"}]) == "a=1; b=2"


def test_cookie_header_empty_is_none():
    assert _cookie_header([]) is None
    assert _cookie_header(None) is None


@pytest.mark.asyncio
async def test_fetch_raw_sends_cookie_header_when_given():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["cookie"] = request.headers.get("cookie")
        return httpx.Response(200, text="<html>ok</html>")

    svc = FirecrawlService()
    svc.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await svc.fetch_raw("https://x/p", cookies=[{"name": "SAML", "value": "tok"}])
    assert seen["cookie"] == "SAML=tok"
    await svc.client.aclose()


@pytest.mark.asyncio
async def test_fetch_raw_no_cookie_header_when_absent():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["cookie"] = request.headers.get("cookie")
        return httpx.Response(200, text="<html>ok</html>")

    svc = FirecrawlService()
    svc.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await svc.fetch_raw("https://x/p")
    assert seen["cookie"] is None
    await svc.client.aclose()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_fetch_raw_cookies.py -v`
Expected: FAIL — `ImportError: cannot import name '_cookie_header'` (and `fetch_raw` has no `cookies` param).

- [ ] **Step 3: Add the helper and the cookie param**

In `backend/app/services/firecrawl.py`, add the helper near the other module-level helpers (e.g. just above `class FirecrawlService` or beside `_resolve_content_engine`):

```python
def _cookie_header(cookies: list[dict] | None) -> str | None:
    """Build a ``Cookie`` request-header value from a realm cookie list."""
    if not cookies:
        return None
    pairs = [f"{c['name']}={c['value']}" for c in cookies if c.get("name")]
    return "; ".join(pairs) or None
```

Replace `fetch_raw` with the cookie-aware version (keep the docstring; only the signature and the request line change):

```python
    async def fetch_raw(self, url: str, cookies: list[dict] | None = None) -> str:
        """Plain GET of a static asset, bypassing Firecrawl's HTML cleaning.

        Used for non-HTML resources a profile needs verbatim — e.g. MadCap Flare's
        ``Data/*.xml``/``Data/Tocs/*.js`` TOC files, which Firecrawl would strip or
        mangle. Sends a browser UA; ``cookies`` (a realm session cookie list)
        are sent as a ``Cookie`` header for authenticated raw_http sources.
        Raises on HTTP error.

        Retries transient failures (429/5xx, connect/read timeouts) with backoff:
        the raw_http content path fetches hundreds of pages, so without this a
        single momentary blip or short-lived rate-limit permanently drops a page,
        and enough of those trip the run's failure-rate guard (observed on a
        700-page MadCap source). Non-transient errors (e.g. 404) still raise at once.
        """
        headers = {"User-Agent": _BROWSER_UA}
        ck = _cookie_header(cookies)
        if ck:
            headers["Cookie"] = ck
        resp = await self._request_with_retry(
            lambda: self.client.get(url, headers=headers, follow_redirects=True),
            what=f"raw GET {url}",
        )
        return resp.text
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_fetch_raw_cookies.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/firecrawl.py backend/tests/test_fetch_raw_cookies.py
git commit -m "feat(extract): cookie-injecting fetch_raw for authenticated raw_http"
```

---

### Task 2: Auth-aware `Scraper.get_raw`

**Files:**
- Modify: `backend/app/services/profiles/scraper.py` (`Scraper.__init__`, `Scraper.get_raw`)
- Test: `backend/tests/test_scraper_auth_cookies.py` (new)

**Interfaces:**
- Consumes: `FirecrawlService.fetch_raw(url, cookies=...)` from Task 1.
- Produces: `Scraper(firecrawl, checkpoint=None, auth_cookies=None)`; `get_raw` forwards `auth_cookies` to `fetch_raw`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_scraper_auth_cookies.py`:

```python
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from app.services.profiles.scraper import Scraper


class _FC:
    def __init__(self):
        self.calls = []

    async def fetch_raw(self, url, cookies=None):
        self.calls.append((url, cookies))
        return "<html>ok</html>"


@pytest.mark.asyncio
async def test_get_raw_forwards_auth_cookies():
    fc = _FC()
    cookies = [{"name": "SAML", "value": "tok"}]
    s = Scraper(fc, auth_cookies=cookies)
    await s.get_raw("https://x/p")
    assert fc.calls == [("https://x/p", cookies)]


@pytest.mark.asyncio
async def test_get_raw_default_no_cookies():
    fc = _FC()
    s = Scraper(fc)
    await s.get_raw("https://x/p")
    assert fc.calls == [("https://x/p", None)]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_scraper_auth_cookies.py -v`
Expected: FAIL — `Scraper()` has no `auth_cookies` kwarg / `get_raw` passes no `cookies`.

- [ ] **Step 3: Thread `auth_cookies` through the Scraper**

In `backend/app/services/profiles/scraper.py`, update `__init__` and `get_raw` (the real `Scraper`, near the top of the file):

```python
    def __init__(self, firecrawl, checkpoint=None, auth_cookies=None):
        self._fc = firecrawl
        self.checkpoint = checkpoint
        self._auth_cookies = auth_cookies
```

```python
    async def get_raw(self, url: str) -> str:
        return await self._fc.fetch_raw(url, cookies=self._auth_cookies)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_scraper_auth_cookies.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/profiles/scraper.py backend/tests/test_scraper_auth_cookies.py
git commit -m "feat(extract): Scraper threads auth cookies into get_raw"
```

---

### Task 3: Content-path selection + extract_source wiring

**Files:**
- Modify: `backend/app/services/firecrawl.py` (`_select_content_path` helper; `_resolve_profile` signature + call; TOC-build scraper; content-routing branch; `_scrape_via_raw_http` cookie threading)
- Test: `backend/tests/test_content_path_selection.py` (new)

**Interfaces:**
- Consumes: `Scraper(..., auth_cookies=...)` (Task 2); `fetch_raw(url, cookies=...)` (Task 1); existing `_resolve_content_engine(source, profile)`.
- Produces: module function `_select_content_path(has_auth: bool, content_engine: str | None, render_engine: str | None) -> str` returning `"raw_http"`, `"browserless"`, or `"firecrawl"`; `_resolve_profile(self, source, auth_cookies=None)`; `_scrape_via_raw_http(..., auth_cookies=None)`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_content_path_selection.py`:

```python
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.firecrawl import _select_content_path


def test_authed_raw_http_uses_raw_http():
    assert _select_content_path(True, "raw_http", None) == "raw_http"


def test_unauthed_raw_http_uses_raw_http():
    assert _select_content_path(False, "raw_http", None) == "raw_http"


def test_authed_non_raw_uses_browserless():
    assert _select_content_path(True, None, None) == "browserless"


def test_browserless_render_engine_uses_browserless():
    assert _select_content_path(False, None, "browserless") == "browserless"


def test_plain_source_uses_firecrawl():
    assert _select_content_path(False, None, None) == "firecrawl"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_content_path_selection.py -v`
Expected: FAIL — `ImportError: cannot import name '_select_content_path'`.

- [ ] **Step 3: Add the selection helper**

In `backend/app/services/firecrawl.py`, add beside `_resolve_content_engine`:

```python
def _select_content_path(
    has_auth: bool, content_engine: str | None, render_engine: str | None
) -> str:
    """Pick the content-scrape path.

    A raw_http profile always uses the raw-HTTP path (cookies are injected when
    authenticated). An authenticated non-raw_http profile, or one that requires
    Browserless rendering, uses Browserless. Everything else uses the Firecrawl
    batch path.
    """
    if content_engine == "raw_http":
        return "raw_http"
    if has_auth or render_engine == "browserless":
        return "browserless"
    return "firecrawl"
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/test_content_path_selection.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Thread auth cookies through discovery + dispatch on the helper**

In `backend/app/services/firecrawl.py`:

(a) `_resolve_profile` — change the signature to accept `auth_cookies` and use it for the root-HTML scraper. At line ~218:

```python
    async def _resolve_profile(self, source, auth_cookies=None):
```

and the scraper construction at ~246:

```python
            scraper = Scraper(self, auth_cookies=auth_cookies)
```

(b) In `extract_source`, `auth_state` is already resolved before profile resolution. Just above the `profile = await self._resolve_profile(source)` call (~1410), derive the cookie list, and pass it to both discovery scrapers:

```python
            auth_cookies = (auth_state or {}).get("cookies")
            profile = await self._resolve_profile(source, auth_cookies=auth_cookies)
```

and the TOC-build scraper at ~1433:

```python
                source.base_url, Scraper(self, checkpoint=checkpoint, auth_cookies=auth_cookies)
```

(c) Replace the content-routing branch (the `if auth_state is not None or getattr(profile, "render_engine", None) == "browserless":` block and its `elif _resolve_content_engine(...) == "raw_http":` / `else` siblings) with a dispatch on the helper. Keep the existing bodies of each branch (the `NeedsLoginError` handling stays inside the browserless branch):

```python
            path = _select_content_path(
                auth_state is not None,
                _resolve_content_engine(source, profile),
                getattr(profile, "render_engine", None),
            )
            if path == "raw_http":
                await self._scrape_via_raw_http(
                    db, source_id, run_pk, url_to_entry, profile, checkpoint,
                    auth_cookies=auth_cookies,
                )
            elif path == "browserless":
                # (existing browserless body: content_spec resolution,
                #  _scrape_via_browserless(..., auth_state=auth_state), and the
                #  NeedsLoginError handler — unchanged)
                ...
            else:
                # (existing Firecrawl batch body — unchanged)
                ...
```

(d) `_scrape_via_raw_http` (~960) — add the param and thread it into the per-page fetch. Signature:

```python
    async def _scrape_via_raw_http(
        self,
        db: AsyncSession,
        source_id: uuid.UUID,
        run_id: uuid.UUID,
        url_to_entry: dict[str, dict],
        profile,
        checkpoint,
        auth_cookies: list[dict] | None = None,
    ) -> None:
```

Inside this method, the per-page fetch is an inner `async def _fetch(url)` whose body calls `await self.fetch_raw(url)`. Change only that one call to pass the cookies:

```python
        async def _fetch(url: str) -> str | None:
            try:
                return await self.fetch_raw(url, cookies=auth_cookies)
```

(Only that single `fetch_raw` call inside `_fetch` changes; keep the rest of `_fetch`'s try/except and the surrounding chunked-gather + sequential-persist loop exactly as-is.)

- [ ] **Step 6: Run the focused + regression tests**

Run: `pytest tests/test_content_path_selection.py tests/test_extract_auth_realm.py tests/test_static_platform_profiles.py -q`
Expected: PASS (the new selection tests, plus the existing auth-realm extraction and static-profile suites — confirming no regression in routing/auth wiring).

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/firecrawl.py backend/tests/test_content_path_selection.py
git commit -m "feat(extract): auth-aware TOC discovery + route authed raw_http via cookie-injected raw path"
```

---

### Task 4: `fern` profile

**Files:**
- Create: `backend/app/services/profiles/fern.py`
- Modify: `backend/app/services/profiles/__init__.py` (register before `generic`)
- Test: `backend/tests/test_profiles_fern.py` (new)

**Interfaces:**
- Consumes: `app.services.profiles.base.TocEntry`; `app.services.profiles.registry`; `app.services.profiles.strategies.parse_sidebar_tree(html, root_url, nav_selector)`; `app.services.profiles.content_scope.scope_content_html`; `app.services.profiles.detector.detect_platform`; `app.services.profiles.scraper.FakeScraper`.
- Produces: `FernProfile` (`name="fern"`, `content_engine="raw_http"`, `detect`, async `build_toc`, `content_config`); module `PROFILE = FernProfile()` registered.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_profiles_fern.py`:

```python
"""Tests for the fern profile (Fern docs, e.g. docs.eon.io).

Fern server-renders the full sidebar (aside.fern-sidebar-desktop, a nested
<ul>/<li> tree) and the article body (.fern-prose) into static HTML, so the
profile runs on the raw_http path (with the realm session injected for
authenticated sources). Hermetic: FakeScraper serves canned HTML.
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

PAGE = """
<html><body>
  <aside class="fern-sidebar-desktop">
    <ul>
      <li><a href="/user-guide/what-is-eon">What Is Eon</a></li>
      <li>
        <a href="/user-guide/access-management/about-access-management">Access Management</a>
        <ul>
          <li><a href="/user-guide/access-management/api-credentials/about-api-credentials">API Credentials</a></li>
        </ul>
      </li>
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
async def test_builds_nested_tree():
    toc = await FernProfile().build_toc(ROOT, _scraper())
    shape = [(e.level, e.title, e.is_article) for e in toc]
    assert shape == [
        (0, "What Is Eon", True),
        (0, "Access Management", False),
        (1, "API Credentials", True),
    ]
    assert all(e.url for e in toc)


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
    assert "Access Management" not in out       # sidebar outside scope
```

Note on the `build_toc` shape: `parse_sidebar_tree` marks a node with a child `<ul>` as a section (`is_article=False`) and a leaf as an article — matching the asserted shape. (If the real Fern markup nests an `<a>`'s children differently, the plan's live validation will catch it; the fixture reflects the captured structure.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_profiles_fern.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.profiles.fern'`.

- [ ] **Step 3: Create the profile**

Create `backend/app/services/profiles/fern.py`:

```python
"""Fern docs profile (full nav tree + bodies server-rendered into static HTML).

Targets documentation built on the Fern framework (buildwithfern.com), e.g.
docs.eon.io. The complete sidebar (aside.fern-sidebar-desktop, a nested
<ul>/<li> tree) and the article body (.fern-prose) are server-rendered into
every page, so the profile runs on the raw_http path. For login-walled Fern
sites the realm session is injected as cookies by the authenticated raw_http
path (see _select_content_path / fetch_raw); the profile itself is unchanged
whether or not auth is in play.
"""

from app.services.profiles import registry
from app.services.profiles.base import TocEntry
from app.services.profiles.strategies import parse_sidebar_tree

_NAV_SELECTOR = "aside.fern-sidebar-desktop"


class FernProfile:
    name = "fern"
    content_engine = "raw_http"

    def detect(self, root_html: str, root_url: str) -> bool:
        # The sidebar hook plus the article hook together are distinctive to Fern.
        return "fern-sidebar" in root_html and "fern-prose" in root_html

    async def build_toc(self, root_url: str, scraper) -> list[TocEntry]:
        try:
            html = await scraper.get_raw(root_url)
        except Exception:
            return []
        return parse_sidebar_tree(html or "", root_url, _NAV_SELECTOR)

    def content_config(self) -> dict:
        return {
            "includeTags": [".fern-prose"],
            # Drop the right-rail "on this page" TOC, breadcrumb/nav, and footer.
            "excludeTags": [".toc-root", "nav", "footer"],
            "onlyMainContent": False,
        }


PROFILE = FernProfile()
registry.register(PROFILE)
```

- [ ] **Step 4: Register the profile**

In `backend/app/services/profiles/__init__.py`, add immediately before the `from app.services.profiles import generic  # noqa: F401,E402` line:

```python
from app.services.profiles import fern  # noqa: F401,E402
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/test_profiles_fern.py -v`
Expected: PASS (all tests, including the registry detect).

- [ ] **Step 6: Regression — profile suite**

Run: `pytest tests/test_profile_interface.py tests/test_static_platform_profiles.py tests/test_profiles_rspress.py tests/test_profiles_prerendered_toc.py -q`
Expected: PASS (new profile registration doesn't disturb others).

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/profiles/fern.py backend/app/services/profiles/__init__.py backend/tests/test_profiles_fern.py
git commit -m "feat(profiles): fern profile (raw_http, server-rendered sidebar + prose)"
```

---

### Task 5: Live validation (controller-run, post-merge)

No code. Validates the feature end-to-end. Run by the controller (needs cluster + DB access).

**Files:** none.

- [ ] **Step 1: Full suite green**

Run: `cd backend && pytest -q`
Expected: PASS (previous total + the new tests).

- [ ] **Step 2: Merge + deploy**

After merge + CI, deploy via the k8s runbook (dump values, bump `image.backend.tag`/`image.frontend.tag` to the new `sha-`, `helm upgrade -f vals.yaml`). Wait for backend + worker rollout.

- [ ] **Step 3: EON — re-acquire session if expired, then extract**

The EON realm (`aca83e46-…`) holds an uploaded `fern_token` that may have expired (~24h). If so, re-upload fresh cookies via `POST /api/auth-realms/aca83e46-.../session`. Then trigger the EON source (`1fe443ba-…`).

- [ ] **Step 4: Verify EON**

Via DB: source `platform='fern'`; the run COMPLETED; `count(*) FROM articles WHERE source_id='1fe443ba-…' AND removed_at IS NULL` ≈ 123 with non-empty `content_markdown`; spot-check one article has clean body (no sidebar/footer bleed, no login-page text). Run is fast (raw-HTTP, not Browserless).

- [ ] **Step 5: Verify AvePoint did not regress**

Re-trigger one AvePoint rspress source (`5b43d03a-…`, realm active). Confirm it now runs via authenticated raw-HTTP (fast, ~1-2 min) and still yields ~108 clean articles (0 login-bleed). If its realm session expired, re-run scripted login first (`POST /api/auth-realms/3f41e8b1-.../login`).

Expected: both pass. If EON TOC count is far below ~123 or content bleeds chrome, return to Task 4 (the profile selectors); if AvePoint regresses, return to Task 3 (routing).

---

## Notes for the implementer

- Run everything from `backend/`. This plan touches only `firecrawl.py`, `scraper.py`, and the two profile files; do not modify other modules.
- `_BROWSER_UA` is an existing module constant in `firecrawl.py` used by `fetch_raw`.
- In Task 3 step 5(c), preserve the existing bodies of the browserless and firecrawl-batch branches verbatim (including the `NeedsLoginError` handler in the browserless branch and the checkpoint/batch logic); only the branch *selection* changes.
