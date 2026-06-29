# Oxygen XML WebHelp extraction profile

**Date:** 2026-06-29
**Status:** Design — approved; pending spec review
**Area:** `backend/app/services/profiles/oxygen_webhelp.py` (new), `backend/app/services/profiles/__init__.py`

A new extraction profile for documentation published with **Oxygen XML WebHelp** (Responsive). First consumer: Rubrik docs (`docs.rubrik.com`, login-walled). Runs on the authenticated raw-HTTP path shipped earlier (cookie-injected `fetch_raw` + `_select_content_path`).

## Problem

Rubrik's `docs.rubrik.com` is an Oxygen XML WebHelp site (markers: `oxygen-webhelp/app/...` assets, `wh_*` classes, `data-tocid`, `role="treeitem"`). No existing profile detects it (`detect_platform` → None). Investigation (live, authenticated) established:

- The on-page TOC (`nav[class*="col-lg-3"]`) is **contextual**: only the current page's branch (~37 nodes) is present; collapsed siblings do not expand to the full tree, so the page-level TOC cannot enumerate the publication.
- There is **no sitemap** (`/sitemap.xml` → 403) and **no separate TOC fetch** on load.
- Oxygen's search index ships a complete page inventory: `…/oxygen-webhelp/app/search/index/htmlFileInfoList.js` — `var htmlFileInfoList = ["relpath.html@@@Title@@@Description", …]`. For Rubrik it has **4,240 unique entries** (the whole RSC SaaS publication), no anchors/duplicates.
- Article content is server-rendered in-page (the DITA topic body, selector `article` == `main` == `.wh_topic_content`), so content scrapes via authenticated raw-HTTP.

## Solution — `oxygen_webhelp` profile

New module `backend/app/services/profiles/oxygen_webhelp.py`, self-registering like the other profiles. `content_engine = "raw_http"`. Generic to Oxygen WebHelp; validated on Rubrik.

### `detect(root_html, root_url) -> bool`

```python
return "oxygen-webhelp" in root_html and "wh_topic_body" in root_html
```

Two distinctive hooks together: the framework asset namespace (`oxygen-webhelp`) and the topic-body host (`wh_topic_body`). Reusable across Oxygen WebHelp sites; no collision with other profiles.

### `content_engine = "raw_http"`

Content (and the inventory file) is fetched with the realm session injected — the existing authenticated raw-HTTP path (`fetch_raw(url, cookies=…)`, routed by `_select_content_path`). Detection of an authenticated source already fetches the root via `get_raw` (PR #103), so `detect` runs on the authenticated page.

### `build_toc(root_url, scraper) -> list[TocEntry]`

1. `html = await scraper.get_raw(root_url)`. Locate the **publication root**: find an `oxygen-webhelp/` reference in the page (regex on a `src`/`href` containing `oxygen-webhelp/`), resolve it against `root_url`, and take the substring up to and including the segment before `oxygen-webhelp/` → `pub_root` (ends with `/`). If none found → `[]`.
2. `raw = await scraper.get_raw(pub_root + "oxygen-webhelp/app/search/index/htmlFileInfoList.js")`. Extract the array literal: match `htmlFileInfoList\s*=\s*(\[.*\])` (DOTALL) and `json.loads` it. On failure → `[]`.
3. For each entry string, split on `"@@@"` → `path` (first part), `title` (second part, fallback to `path`). Build `TocEntry(title=title, url=urljoin(pub_root, path), level=<path-depth>, is_article=True)`. `path-depth` = `len(path.strip("/").split("/")) - 1` (e.g. `OLVM/x.html` → 1, `index.html` → 0, `saas/common/x.html` → 2). Order = file order. De-dupe by resolved URL.

`parent_url` left default `None`; the pipeline's `_resolve_toc_parents` nests by `level`.

### `content_config() -> dict`

```python
return {
    "includeTags": ["article"],
    "excludeTags": [".related-links", "nav", "header", "footer", ".wh_breadcrumb"],
    "onlyMainContent": False,
}
```

`article` is the Oxygen DITA topic body; the excludes drop the contextual TOC nav, breadcrumb, related-links, and page header/footer. Applied by the raw-HTTP path's `scope_content_html`.

## Module changes

- **`backend/app/services/profiles/oxygen_webhelp.py`** (new) — the profile.
- **`backend/app/services/profiles/__init__.py`** — import `oxygen_webhelp` before `generic` so it registers and detection sees it.

No config, schema, or migration changes. The authenticated raw-HTTP capability and the Cookie-Editor upload / expiry UI already exist.

## Error handling

- No `oxygen-webhelp` reference, or `htmlFileInfoList.js` missing/unparseable → `build_toc` returns `[]` (run reports 0 scrapable pages — loud, not a silent partial).
- Content phase: the existing `raw_http` failure-rate guard fails the run loudly (`RawContentScrapeError`) if too many page GETs fail (e.g. an expired session mid-run returning login pages).
- **Large corpus vs short token:** 4,240 authenticated GETs against Rubrik's ~1h session token may outlast the token. The run is **checkpoint-resumable** (the raw-HTTP path records completed URLs), so re-uploading a fresh cookie and re-triggering resumes from where it stopped. The new expiry-block trigger guard prevents starting a run on an already-expired session.

## Testing

Unit (pytest, hermetic; `FakeScraper` with `raw_by_url`):
- `detect`: True when both `oxygen-webhelp` and `wh_topic_body` are present; False if either is missing.
- `build_toc`: a fixture page containing an `oxygen-webhelp/` asset ref, plus a small `htmlFileInfoList.js` (`var htmlFileInfoList = ["a.html@@@A@@@d", "sec/b.html@@@B@@@d", "sec/sub/c.html@@@C@@@d"]`) wired into `raw_by_url` at the derived inventory URL → returns 3 entries with correct titles, absolute URLs (resolved against `pub_root`), and levels (0,1,2); duplicate paths de-duped; order preserved.
- `build_toc` returns `[]` when the page has no `oxygen-webhelp` ref, and when the inventory file is missing/unparseable.
- `content_config`: `scope_content_html` over a fixture keeps the `article` body text and drops `.wh_breadcrumb` / `.related-links` / `nav` / `footer` content.

Live validation: a `docs.rubrik.com` source with a fresh-cookie realm → auto-detects `oxygen_webhelp`, TOC ≈ 4,240 entries, clean article content; expect a fresh-cookie + resume cycle given the corpus size.

## Out of scope

- Section/landing-page synthesis (Oxygen directory "sections" rarely have own pages; URL-path levels suffice, consistent with the rspress/fern profiles).
- The contextual on-page TOC tree (superseded by the inventory file).
- Any change to the authenticated raw-HTTP capability or the auth UI (already shipped).
