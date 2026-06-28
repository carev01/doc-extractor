# Rspress extraction profile

**Date:** 2026-06-28
**Status:** Design — approved; pending spec review
**Area:** `backend/app/services/profiles/rspress.py` (new), registered via `app/services/profiles/__init__.py`

A new extraction profile for documentation sites built on the [Rspress](https://rspress.dev) static-site framework. Validated against AvePoint Learn (`learn.avepoint.com`), written to be reusable for any Rspress docs.

## Problem

AvePoint Learn docs (e.g. the Cloud Backup for Microsoft 365 guide) are a public Rspress site that no existing profile detects. The generic LLM fallback fails on it in two ways, both confirmed live:

1. **TOC collapses to 1 of 109 pages.** Rspress server-renders the *complete* nav tree into the static HTML of every page, but after JS hydration it collapses the sidebar to only the current page. Any path that renders JS (LLM/generic, Firecrawl `/scrape`) sees a 1-link sidebar. The full tree (109 `/m365/*.html` links for that guide) exists only in the **raw, unrendered** HTML.
2. **Content comes back empty.** The LLM-derived content selector (a hashed CSS-module class) matched nothing under Firecrawl's own browser render.

Both are solved by running entirely on the existing `raw_http` content path — a plain GET (no JS, no Browserless, no Firecrawl) plus local BeautifulSoup scoping. Verified: the article body is present in the raw HTML under `.rspress-doc` (4307 chars raw vs 4303 rendered — identical), so both the TOC and the bodies are static.

This is the same situation the existing `prerendered_toc` (Veeam) profile already handles; Rspress differs only in its detect fingerprint, sidebar markup, and content selectors.

## Site fingerprint (verified on AvePoint Learn)

- Distinctive markers in every page's raw HTML: `rspress-doc`, `rspress-sidebar`, `rspress-nav`, `rspress-doc-footer`, `rspress-local-toc-container`. Hashed CSS-module / Tailwind classes (e.g. `menuItem_ac22e`, `docLayout_af141`) — not stable, must not be keyed on.
- Sidebar (`aside.rspress-sidebar`) contains **no `<ul>/<li>`**. It is a flat DOM-ordered sequence of `<h2>` group headers and `<a>` links (110 anchors incl. the logo; 109 real article links + 23 `<h2>` section headers for the M365 guide).
- Hierarchy is encoded by **URL path depth**, which correlates with the visual indent:
  - `whats-new-in-this-release.html` → level 0
  - `faqs/license-and-subscription.html` → level 1 (under the "FAQs" section)
  - `about-…-microsoft-365/avepoint-cloud-backup/multigeo-support.html` → level 2
- Section `<h2>` headers are inconsistent landing pages: `faqs.html`, `required-permissions.html`, `enable-the-backup-service.html` all **302-redirect** (label-only, no own page), while `about-…/avepoint-cloud-backup.html` is a real **200** page. So sections must be representable as label-only structural nodes, with the occasional real page still extracted as an ordinary article when it appears as an anchor.

## Solution — a generic `rspress` profile

New module `backend/app/services/profiles/rspress.py` implementing the `ExtractionProfile` protocol (`detect`, `build_toc`, `content_config`), self-registering on import via `app/services/profiles/__init__.py`. Class attribute `content_engine = "raw_http"`. No changes to the worker, pipeline, or any other module.

### `detect(root_html, root_url) -> bool`

```python
return "rspress-doc" in root_html and "rspress-sidebar" in root_html
```

Two hooks together (same belt-and-suspenders approach as `prerendered_toc`) — distinctive to Rspress, reusable across Rspress sites, and won't collide with other profiles. Auto-detection runs the registered profiles' `detect()` in registry order; `rspress` keys are specific enough that order does not matter.

### `build_toc(root_url, scraper) -> list[TocEntry]`

```python
html = await scraper.get_raw(root_url)   # plain GET, no render
# parse aside.rspress-sidebar
```

Parse `aside.rspress-sidebar` and walk its `<h2>` and `<a>` descendants in **DOM order** (which is DFS pre-order — a parent always precedes its children). For each:

- **`<a href>`** that is a real menu link → `TocEntry(title=text, url=urljoin(root_url, href), level=<path-depth>, is_article=True, parent_url=None)`.
- **`<h2>`** section header → `TocEntry(title=text, url=None, level=<section-level>, is_article=False, parent_url=None)`.

**Level derivation (strict hierarchy fidelity):**

- **Guide root** = the longest common path-directory prefix shared by all the sidebar's article URLs (more robust and generic than assuming a fixed first segment). For the M365 guide every article is under `/m365/`, so the root is `/m365/`. Concretely: collect all article URL paths, take their longest common `/`-delimited directory prefix; for each article strip that prefix, split the remainder on `/`, and `level = len(remaining_segments) - 1`.
- **Article `level`** = URL path depth as above (0-based): 1 segment → 0, 2 → 1, 3 → 2.
- **Section `<h2>` `level`** = `(level of the next following article anchor) - 1`, clamped to ≥ 0. (A section sits exactly one level above the children it introduces.)

**Parent linkage** is left to the pipeline: `firecrawl._resolve_toc_parents` falls back to "most recent prior entry one level up" using a `level → index` map that records **every** emitted entry, including `url=None` section nodes. So emitting entries in DOM (pre-)order with correct `level` values makes section nodes parent their children automatically; `parent_url` is not set.

**Exclusions:**

- The site logo anchor (`href` resolving to the site root, e.g. `/index.html`, which sits outside the menu list) is skipped.
- Any anchor that is not a menu item (not within the sidebar's menu-item structure — e.g. a "request an article" / external chrome link) is skipped. Concretely: keep only anchors whose resolved URL is under the guide-root prefix; drop the rest. This also drops cross-guide links.

**Scrapable count:** only `url`-bearing entries are scraped (the pipeline computes `articles_total` as the count of entries with a URL), so the ~109 articles are fetched and the label-only section nodes are structural only.

### `content_config() -> dict`

```python
return {
    "includeTags": [".rspress-doc"],
    "excludeTags": [".rspress-local-toc-container", ".rspress-doc-footer", "nav"],
    "onlyMainContent": False,
}
```

On the `raw_http` path, `firecrawl._scrape_via_raw_http` applies these include/exclude selectors via the generic `scope_content_html(raw, url, include, exclude)` — no bespoke `extract_content_html` needed. `.rspress-doc` holds the full body in raw HTML; the right-rail mini-TOC (`.rspress-local-toc-container`), the prev/next + edit footer (`.rspress-doc-footer`), and any in-doc `nav` are dropped.

## Module changes

- **`backend/app/services/profiles/rspress.py`** — new file: the profile + a small sidebar-parsing helper. The `<h2>`/`<a>` walk and level math are bespoke (no `<ul>/<li>`, so `strategies.parse_sidebar_tree` does not apply); keep the helper local to this module unless a second consumer appears (YAGNI).
- **`backend/app/services/profiles/__init__.py`** — import the new module so it self-registers (same one-line pattern as the other profiles).

No config, schema, migration, or pipeline changes.

## Error handling

- **Missing/empty sidebar** → `build_toc` returns `[]`. The run then reports 0 scrapable pages (loud), rather than silently succeeding with partial data — consistent with other profiles.
- **Per-page fetch/scope failures** → handled by the existing `raw_http` guard: once `raw_http_min_attempts` pages are attempted, a failure fraction above `raw_http_max_failure_rate` fails the run with `RawContentScrapeError` instead of completing partial.
- **Non-AvePoint Rspress site with different sidebar internals** → the parser degrades to anchors-only (sections may be missed, but every real page is still emitted via its `<a>`), so extraction still works; hierarchy may be flatter. Refinement deferred until a second Rspress site exists (YAGNI).

## Testing

Unit tests (pytest), using saved AvePoint Learn raw-HTML fixtures (the M365 about page, which carries the full sidebar, plus one article page for content scoping). Add fixtures under `backend/tests/fixtures/`.

- **`detect`**: True for the AvePoint fixture; False for a Veeam (`js-page-toc`) fixture, a Docusaurus snippet, and an arbitrary `<html>` with neither hook.
- **`build_toc`** (fixture parsed through a stub scraper whose `get_raw` returns the fixture):
  - emits 109 article entries (all `is_article=True`, all with URLs under the guide root) plus the section `<h2>` nodes (`is_article=False`, `url=None`);
  - the logo / non-menu anchors are excluded (no entry for `/index.html`);
  - levels are correct on representative entries: `whats-new-in-this-release.html` → 0, `faqs/license-and-subscription.html` → 1, `about-…/avepoint-cloud-backup/multigeo-support.html` → 2;
  - a label-only section ("FAQs") is emitted as `url=None`, `is_article=False`, at the level one above its children;
  - entries are in DOM order (pre-order: each section precedes its children).
- **`_resolve_toc_parents` integration**: feed the `build_toc` output (as the dicts the pipeline builds) through `_resolve_toc_parents` and assert the FAQs children resolve their parent index to the "FAQs" section node (verifies the `url=None` + level mechanism end-to-end).
- **`content_config` / scoping**: run `scope_content_html` with the profile's include/exclude over the article fixture; assert the article body text is present and the mini-TOC / footer / nav text is absent.

**Live validation:** add the AvePoint M365 source (already created in prod: source `5b43d03a-75bf-42d4-8d21-e6048673a87c`) and re-extract → auto-detects `rspress`, ~109 pages, clean Markdown bodies (no nav/sidebar/footer bleed), nested TOC; spot-check that `about-…/avepoint-cloud-backup.html` (a section that is also a real page) is extracted and that a deep page (level 2) is correctly nested.

## Rollout

After merge + CI: deploy (helm, pin new `sha-`), then re-extract the AvePoint M365 source. No schema change, no data migration. The existing source's `profile_config`/`platform` were reset to NULL after the failed LLM run, so it will cleanly auto-detect `rspress` on re-extraction.
