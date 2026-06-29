# Oxygen XML WebHelp extraction profile (Rubrik) — inventory TOC + post-process hierarchy + WAF pacing + expiry notification

**Date:** 2026-06-29
**Status:** Design — approved (direction); pending spec review
**Area:** `backend/app/services/profiles/oxygen_webhelp.py` (new), `backend/app/services/profiles/__init__.py`, `backend/app/services/firecrawl.py` (raw-HTTP path: fragment capture + post-process TOC rebuild + pacing), `backend/app/core/config.py` (pacing knobs)

A new `oxygen_webhelp` profile for Oxygen XML WebHelp docs, plus the pipeline support it needs. First/only consumer: Rubrik (`docs.rubrik.com`), which is login-walled **and** WAF-protected. Generic to Oxygen WebHelp where applicable.

## Findings (live investigation, authenticated)

- Platform: **Oxygen XML WebHelp Responsive** — `oxygen-webhelp/app/...` assets, `wh_*` classes (`wh_publication_toc`, `wh_topic_body`, `wh_breadcrumb`), `data-tocid`, `role="treeitem"`. Article content is server-rendered in-page in `article` (== `main` == `.wh_topic_content`).
- The on-page TOC (`#wh_publication_toc`) is **contextual**: it contains the current page's **full ancestor chain → the page → its direct children**, plus collapsed top-level siblings (each `li[role=treeitem]` has `data-tocid`, an `<a href>`, and nests children via `<ul>`). No single page or file holds the whole tree; there is **no** sitemap and **no** standalone TOC file (all 403).
- Oxygen's search index ships a **complete page inventory**: `<pub_root>/oxygen-webhelp/app/search/index/htmlFileInfoList.js` = `var htmlFileInfoList = ["relpath.html@@@Title@@@Description", …]` — **4,240 unique pages** for Rubrik, strictly alphabetical by path. `pub_root` = the directory containing `oxygen-webhelp/` (for Rubrik RSC: `https://docs.rubrik.com/en-us/saas/`).
- **WAF:** `docs.rubrik.com` returns **401 under request bursts** — for headless Chrome (Browserless is 401'd even for a single page) **and** for plain authenticated GETs under load. Single, spaced GETs succeed (200). So: browser tree-expansion is not viable; raw-HTTP works but must be **paced**.
- **Session:** the `SAML_TOKEN` cookie is **~1h-lived and rotating**; a 4,240-page run will outlast it and must resume.

## Design overview

Get a complete, reliable page set from the inventory file (raw-HTTP), scrape content via the authenticated raw-HTTP path, capture each page's contextual TOC fragment **during** that scrape (no extra fetches), and **post-process** the fragments into the authored hierarchy, rewriting the persisted TOC. Pace the raw-HTTP path so the WAF doesn't 401 us.

### 1. `oxygen_webhelp` profile (`backend/app/services/profiles/oxygen_webhelp.py`)

- `name = "oxygen_webhelp"`, `content_engine = "raw_http"`.
- `detect(root_html, root_url)` → `"oxygen-webhelp" in root_html and "wh_publication_toc" in root_html`.
- `build_toc(root_url, scraper)`:
  1. `get_raw(root_url)`; derive `pub_root` by resolving an `…/oxygen-webhelp/` asset reference against `root_url` (substring up to and including the segment before `oxygen-webhelp/`). None found → `[]`.
  2. `get_raw(pub_root + "oxygen-webhelp/app/search/index/htmlFileInfoList.js")`; extract the array literal (`htmlFileInfoList\s*=\s*(\[.*\])`, DOTALL) and `json.loads`. Unparseable → `[]`.
  3. One `TocEntry` per entry: split on `@@@` → `(path, title)`; `url = urljoin(pub_root, path)`, `level` = URL path depth (placeholder — rewritten in step 3 of the pipeline), `is_article=True`. De-dupe by URL; order = file order.
- `content_config()` → `includeTags=["article"]`, `excludeTags=[".related-links","nav","header","footer",".wh_breadcrumb"]`, `onlyMainContent=False`.
- **Pacing knobs (class attributes read by the raw-HTTP path):** `raw_http_concurrency = 2`, `raw_http_retry_statuses = (401, 429, 502, 503, 504)`, `raw_http_request_delay = 0.3` (seconds between a worker's requests).
- **Post-process TOC hooks:**
  - `toc_fragment_selector = "#wh_publication_toc"` — tells the raw-HTTP path to capture this element's outerHTML from each page before content scoping.
  - `rebuild_toc(fragments: list[tuple[str, str]], root_url: str) -> list[TocEntry]` — given `(page_url, fragment_html)` pairs, parse each fragment's `li[role=treeitem]` tree (`data-tocid`, nested `<ul>`, `<a href>`, title), union into a global `tocid → {url, title, parent_tocid, order}` map (later fragments fill gaps; the page that "owns" a node — where it is the expanded current node — is authoritative for its children/order), then emit `TocEntry` objects in DFS pre-order with `level` and `parent_url` from the assembled tree. URLs resolved against `pub_root` (re-derived from `root_url` the same way as `build_toc`).

### 2. Raw-HTTP fragment capture (`firecrawl.py::_scrape_via_raw_http`)

When `getattr(profile, "toc_fragment_selector", None)` is set: for each fetched page's full HTML (before `extract_body` scoping), extract that selector's outer HTML via BeautifulSoup and **persist it on the page's `Article` row** in a new nullable column `articles.toc_fragment` (set alongside `content_markdown`/`content_html` in the same upsert). Persisting (not just in-memory) makes the post-process **run-independent**: the ~1h rotating token means content completes over several resumed runs, and the rebuild must see every page's fragment regardless of which run fetched it. Capture adds parsing + one column write — no extra requests. Pages already complete (skipped by the checkpoint on a resume) keep their previously-stored fragment.

### 3. Post-process TOC rebuild (`firecrawl.py`, after the raw-HTTP content loop)

After `_scrape_via_raw_http` completes the content set successfully (not cancelled/failed) and the profile defines `rebuild_toc`:
1. Load `(url, toc_fragment)` for **all** non-removed `Article` rows of the source (run-independent), filtering out null fragments.
2. Call `profile.rebuild_toc(fragments, source.base_url)` → ordered `TocEntry` list with the authored hierarchy.
3. Re-persist the TOC: reuse the existing phase-1 persistence (delete `TOCEntry` rows for the source, `_resolve_toc_parents` for parent linkage, re-insert with `level`/`sort_order`/`parent_id`), then **re-link** each stored `Article` to its new `toc_entry_id` by URL (articles already exist; only TOC linkage/order changes).
4. If `rebuild_toc` returns `[]` (no usable fragments), leave the inventory-derived TOC in place (degrade, don't destroy) and log a warning.

This runs only for raw-HTTP profiles that opt in (`rebuild_toc` present); all other profiles/paths are unchanged.

### 4. WAF-aware pacing (`firecrawl.py` raw-HTTP path + `fetch_raw` + `config.py`)

- The raw-HTTP content loop reads `getattr(profile, "raw_http_concurrency", settings.raw_http_concurrency)` for its chunk size, and applies `getattr(profile, "raw_http_request_delay", 0)` as a small sleep between a worker's requests.
- `fetch_raw(url, cookies=None, retry_statuses=None)` — extend the retry set: statuses in `retry_statuses` (default the existing transient set) are retried with backoff. The raw-HTTP path passes `getattr(profile, "raw_http_retry_statuses", None)` so `oxygen_webhelp` retries **401** (WAF throttle) with backoff. Retries are bounded (existing `TRANSIENT_RETRIES`), so a genuinely dead session still fails out and trips the failure-rate guard rather than looping forever.
- New settings (defaults preserve current behavior): none required if the profile attributes drive it; `config.py` gains documented defaults only if we want global overrides (`raw_http_concurrency` already exists).

### 5. Session-expiry notification (generic webhook)

A long, multi-resume run shouldn't require watching the UI. Add a small, **general** notification (benefits every authenticated source, not just Rubrik): when a run stops because the source's realm session has expired, mark the realm `EXPIRED` and POST to a configured webhook so the user knows to upload a fresh cookie.

- **Setting:** `notify_webhook_url` (`DOCEXTRACTOR_NOTIFY_WEBHOOK_URL`; blank = disabled). Stored in the k8s secret (a URL may embed a token).
- **Helper:** `app/services/notify.py` — `async def notify(title: str, message: str, **fields) -> None`. If the URL is set, best-effort `httpx.post` of a JSON body `{"title","message","text","content", **fields}` (the `text`/`content` keys make it render in Slack/Discord/ntfy/generic receivers). Swallows and logs any error — **never** affects the run. No-op when unset.
- **Auto-pause on expiry (not fail):** the pipeline already supports pause/resume — `RunStatus.PAUSED` keeps the resume checkpoint (not a failure), and `POST /runs/{id}/resume` re-queues a paused run so the worker continues from the checkpoint. On mid-run session expiry we **pause** rather than fail, so the user's flow is: *expire → auto-pause + notify → upload fresh cookie → Resume → continue from where it stopped.*
- **Trigger points** (fire once when a run auto-pauses on expiry):
  - Raw-HTTP path: when page failures are predominantly `401` **and** the source's `auth_realm` session is expired (`session_expired(realm)`), stop the scrape via a **pause** control (the existing `RunControlSignal(action="pause")` semantics: set `RunStatus.PAUSED`, keep the checkpoint) instead of raising `RawContentScrapeError`; then `invalidate(db, realm, EXPIRED, …)` and `notify("Session expired", "Realm '<name>' expired during extraction of '<source>' — the run is PAUSED. Upload a fresh cookie and hit Resume to continue.")`. Non-auth failures still fail loudly via the existing guard.
  - Browserless path: the existing `NeedsLoginError` handler currently fails the run; change it to **pause** (keep checkpoint) + `invalidate(EXPIRED)` + `notify(...)`, matching the raw-HTTP behavior.
- The message names the realm + source and states the remedy (upload a fresh cookie, hit Resume; the run continues from the checkpoint).
- **Resume:** the user uploads a fresh cookie (Cookie-Editor upload UI) and calls `POST /runs/{id}/resume` (existing). The worker re-claims the paused run; the raw-HTTP checkpoint skips completed pages; the post-process TOC rebuild runs when the content set finally completes.

## Module changes

- `backend/app/services/profiles/oxygen_webhelp.py` (new) — profile + `rebuild_toc` + pacing attrs.
- `backend/app/services/profiles/__init__.py` — register before `generic`.
- `backend/app/services/firecrawl.py` — `fetch_raw` `retry_statuses` param; `_scrape_via_raw_http` fragment capture (write `toc_fragment`) + per-profile concurrency/delay; post-content `rebuild_toc` + TOC re-persist + article re-link.
- `backend/app/models/article.py` (or wherever `Article` is defined) — new nullable `toc_fragment: Mapped[str | None]` column.
- `backend/alembic/versions/<new>.py` — migration adding `articles.toc_fragment` (nullable text).
- `backend/app/services/notify.py` (new) — best-effort webhook `notify(...)`.
- `backend/app/core/config.py` — `notify_webhook_url` (+ optional documented pacing defaults).
- `backend/app/services/firecrawl.py` — fire `invalidate(EXPIRED)` + `notify(...)` on raw-HTTP auth failure; add `notify(...)` to the browserless `NeedsLoginError` handler.
- `deploy/helm/docextractor/templates/secret.yaml` + `values.yaml` — `DOCEXTRACTOR_NOTIFY_WEBHOOK_URL` wiring (blank default).

One DB schema addition: a nullable `articles.toc_fragment` text column (+ alembic migration). Reuses `toc_entries` otherwise.

## Error handling

- No `oxygen-webhelp` ref / unparseable inventory → `build_toc` returns `[]` (loud, 0 pages).
- WAF 401 under load → retried with backoff per `raw_http_retry_statuses`. Persistent 401 with an **expired** realm session → **auto-pause** (not fail) + EXPIRED + notify (see Component 5). Persistent failures **not** attributable to auth → failure-rate guard fails the run loudly. The expiry-block trigger guard (already shipped) prevents starting on an already-expired session.
- Session expires mid-run → auto-**pause** (checkpoint kept); **resume**: upload a fresh cookie and hit Resume (`/runs/{id}/resume`) — the raw-HTTP checkpoint skips completed pages, whose `toc_fragment` is already stored. The TOC rebuild on the finishing run reads stored fragments for all pages, so it is independent of run boundaries.
- `rebuild_toc` yields nothing → keep inventory TOC, warn.

## Testing

Unit (pytest, hermetic; `FakeScraper`):
- `detect` (both hooks / neither).
- `build_toc`: fixture page with an `oxygen-webhelp/` ref + a small `htmlFileInfoList.js` → correct entries/urls/placeholder-levels; unparseable/missing → `[]`.
- `rebuild_toc`: a set of fixture fragments (e.g. 3 pages whose contextual TOCs overlap to describe a 3-level tree) → correct DFS-ordered entries with proper `level`/`parent_url`; missing fragments → `[]`.
- `_select_content_path`/raw-HTTP: a profile with `toc_fragment_selector` triggers fragment capture; concurrency/delay/`retry_statuses` are read from the profile (assert via a mocked fetch recording calls); 401 retried when in `retry_statuses`, not otherwise.
- `content_config` scoping keeps `article` body, drops `.wh_breadcrumb`/`.related-links`/nav/footer.
- `notify`: posts the JSON payload when `notify_webhook_url` is set (assert via mocked transport), is a no-op when unset, and swallows a POST error without raising.
- expiry handling: a raw-HTTP scrape whose failures are 401s **and** whose realm session is expired sets the run `PAUSED` (checkpoint kept), marks the realm `EXPIRED`, and calls `notify` — it does NOT fail the run; a non-auth failure (or no realm) still fails loudly via the existing guard and does not notify.

Live validation (Rubrik, fresh cookie): auto-detect `oxygen_webhelp`; paced raw-HTTP scrape (low concurrency) avoids WAF 401s; content clean; post-process produces a nested TOC matching the on-site hierarchy on spot-checked branches. Expect multiple fresh-cookie/resume cycles for the full 4,240 pages.

## Risks / sequencing

- **WAF thresholds are unknown**; concurrency=2 + 0.3s delay + 401-backoff is a starting point to tune during live validation (the profile attributes make this easy to adjust).
- **Effort:** this is the largest of the auth features (profile + `toc_fragment` column/migration + raw-HTTP capture/pacing + post-process rebuild). The plan should sequence it so the **profile + pacing + content extraction** land and are proven against the WAF/token first, then the **fragment capture + post-process hierarchy** — each independently testable.

## Out of scope

- Browser tree-expansion for Rubrik (WAF-blocked).
- Changes to the auth UI / authenticated raw-HTTP capability (already shipped).
