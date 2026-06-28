# Authenticated raw-HTTP extraction + Fern profile

**Date:** 2026-06-28
**Status:** Design — approved; pending spec review
**Area:** `backend/app/services/firecrawl.py`, `backend/app/services/profiles/scraper.py`, `backend/app/services/profiles/fern.py` (new), `backend/app/services/profiles/__init__.py`

Enables extracting login-walled documentation whose **root/TOC page is itself gated**, via the fast raw-HTTP path with the realm session injected as cookies. First consumer: EON.io docs (`docs.eon.io`), a Fern-framework site. Also migrates the existing AvePoint (rspress) authenticated sources onto this faster path.

## Problem

Authenticated extraction injects the realm session **only during content scraping** (`_scrape_via_browserless(..., auth_state=...)`). TOC discovery and profile resolution build their `Scraper` with **no** auth state, so `profile.build_toc(...)` and `detect_platform(...)` fetch the root **unauthenticated**. This works only when the root/landing page is public — true for AvePoint Learn (its landing page is public; rspress read the full sidebar there) but **false** for EON, whose root (`/user-guide/what-is-eon`) 302-redirects to login. So EON's TOC discovery would parse the login page. See `[[auth-toc-discovery-gap]]`.

Separately, authenticated sources are **forced** onto the Browserless content path (`if auth_state is not None: _scrape_via_browserless`). For sites that server-render both the nav tree and the article bodies (Fern, rspress), a plain authenticated GET returns everything — far faster than rendering ~100+ pages in Browserless (AvePoint's auth run took ~20+ min). Verified live: an authenticated raw GET of `docs.eon.io` (cookie `fern_token`) returns 515 KB with 123 sidebar links and the full body; no JS needed.

## Solution — authenticated raw-HTTP

Inject the realm's session cookies into the raw fetch, make TOC discovery auth-aware, and route authenticated `raw_http` profiles through the (now cookie-injecting) raw path for both TOC and content. Add a `fern` profile as the first new consumer.

### 1. Cookie-injecting raw fetch

- `FirecrawlService.fetch_raw(url, cookies=None)` — when `cookies` is provided (a list of `{"name","value",...}` dicts from the realm `state_snapshot["cookies"]`), send them as a `Cookie: name=value; …` request header on the GET. No cookies → unchanged behavior. Retry/back-off logic unchanged.
- A small helper `_cookie_header(cookies) -> str | None` builds the header value from the cookie list (name=value pairs joined by `; `), returning None for an empty/missing list.

### 2. Auth-aware Scraper

- `Scraper.__init__(self, firecrawl, checkpoint=None, auth_cookies=None)` stores `auth_cookies`.
- `Scraper.get_raw(url)` calls `self._fc.fetch_raw(url, cookies=self._auth_cookies)`.
- Other `Scraper` methods are unchanged (the raw_http profiles this enables use only `get_raw`).

### 3. extract_source wiring

`auth_state` is already resolved early via `realm_manager.ensure_session` (a `{"cookies": [...], "origins": [...]}` dict). Derive `auth_cookies = (auth_state or {}).get("cookies")` and pass it when constructing the two discovery-phase scrapers:

- profile resolution: `Scraper(self, auth_cookies=auth_cookies)` (the call near the root-HTML fetch in `_resolve_profile` — thread `auth_cookies` through `_resolve_profile`'s signature).
- TOC build: `Scraper(self, checkpoint=checkpoint, auth_cookies=auth_cookies)`.

### 4. Content-path routing

Replace the current "authenticated ⇒ Browserless" gate so the raw_http path wins for raw_http profiles:

```
if auth_state is not None and _resolve_content_engine(source, profile) == "raw_http":
    await self._scrape_via_raw_http(..., auth_cookies=auth_cookies)   # cookie-injected
elif auth_state is not None or render_engine == "browserless":
    await self._scrape_via_browserless(..., auth_state=auth_state)     # unchanged
elif _resolve_content_engine(...) == "raw_http":
    await self._scrape_via_raw_http(...)                               # unchanged (unauthed)
else:
    ... batch Firecrawl ...                                            # unchanged
```

- `_scrape_via_raw_http(..., auth_cookies=None)` threads `auth_cookies` into each page's `fetch_raw`.
- Net effect: an authenticated rspress/fern source uses authenticated raw-HTTP for content; non-raw_http authenticated profiles (none today) keep Browserless+auth; all unauthenticated paths are byte-for-byte unchanged.

### 5. `fern` profile (`backend/app/services/profiles/fern.py`)

Mirrors `prerendered_toc`/`rspress`: a `raw_http` profile whose whole nav tree and bodies are server-rendered.

- `name = "fern"`, `content_engine = "raw_http"`.
- `detect(root_html, root_url)` → `"fern-sidebar" in root_html and "fern-prose" in root_html` (two distinctive Fern hooks together; `buildwithfern`/`data-fern` also present but these two are the stable structural ones).
- `build_toc(root_url, scraper)` → `raw = await scraper.get_raw(root_url)`; the Fern sidebar `aside.fern-sidebar-desktop` is a real nested `<ul>/<li>` tree (132 anchors verified), so reuse `strategies.parse_sidebar_tree(raw, root_url, "aside.fern-sidebar-desktop")` — no bespoke parser. Empty/missing sidebar → `[]`.
- `content_config()` → `includeTags=[".fern-prose"]`, `excludeTags=[".toc-root", "nav", "footer"]` (drop the right-rail mini-TOC, breadcrumb/nav, and footer), `onlyMainContent=False`. (The plan verifies the page `<h1>` is captured by `.fern-prose`; if Fern renders the heading just outside it, widen the include to the `article` body wrapper.)
- Register in `profiles/__init__.py` before `generic`.

## Module changes

- **`firecrawl.py`** — `fetch_raw` cookie param + `_cookie_header` helper; `_resolve_profile` + TOC-build scrapers built with `auth_cookies`; content-routing branch; `_scrape_via_raw_http` cookie threading.
- **`scraper.py`** — `Scraper` `auth_cookies` param; `get_raw` forwards it.
- **`fern.py`** (new) — the profile.
- **`profiles/__init__.py`** — register `fern`.

No schema, migration, or config changes.

## Error handling

- **Expired session:** an authenticated raw GET of a gated page returns the login page (HTTP 200 but login HTML). The profile's content scoping (`.fern-prose` / `.rspress-doc`) matches nothing → the page counts as failed; once enough fail, the existing `raw_http` failure-rate guard (`RawContentScrapeError`) fails the run loudly instead of silently producing login-page articles. (Marking the realm EXPIRED on this path is a future refinement; the loud failure is sufficient for v1.)
- **Empty/missing sidebar in build_toc** → `[]` (run reports 0 scrapable pages, loud).
- **Unauthenticated sources** — `auth_cookies` is None everywhere; behavior is unchanged.

## Testing

Unit (pytest; hermetic):
- `_cookie_header`: builds `a=1; b=2` from a cookie list; None for `[]`/None.
- `fetch_raw`: with `cookies=[...]` the GET carries the `Cookie` header (assert via a mocked transport / captured request); without, no Cookie header.
- `Scraper.get_raw` forwards `auth_cookies` to `fetch_raw`.
- routing: an authenticated source with a `raw_http` profile dispatches to `_scrape_via_raw_http` with cookies (not `_scrape_via_browserless`); an authenticated non-raw_http profile still dispatches to Browserless; an unauthenticated raw_http source is unchanged.
- `fern` profile: `detect` true on the captured Fern fixture, false when either hook is absent; `build_toc` returns the nested tree (article entries with URLs + section nodes) from `aside.fern-sidebar-desktop`; missing sidebar → `[]`; `content_config` scoping keeps the body and drops the right-rail TOC/nav/footer.

Live validation:
- EON: trigger the existing source (`1fe443ba-…`, realm `aca83e46-…`) → auto-detects `fern`, ~123 pages, clean bodies, nested TOC.
- AvePoint: re-extract one rspress source (e.g. `5b43d03a-…`) → now runs via authenticated raw-HTTP (fast), still ~108 pages with clean content (no regression).

## Out of scope

- **Rubrik** — content is in-page but the **TOC is not** (loads separately), so it needs its own discovery strategy and profile; it will reuse this authenticated-raw-HTTP capability but is a separate spec. Its session token is also short-lived (~1h).
- Realm auto-EXPIRED marking on the raw path, and any frontend for realm reassignment.
