# Generalized Extraction via Platform Profiles — Design

**Date:** 2026-06-18
**Status:** Approved (design); pending implementation plan
**Goal addressed:** Make extraction work across many documentation sites, not just Commvault. Today TOC discovery and content-scoping are hardcoded to Commvault's DOM (`#nav .nav-group …`, `includeTags:["#doc"]`). Replace that with a pluggable, auto-detected **platform-profile** architecture.

## Survey that grounds this design

11 real targets were scraped and fingerprinted. **Every one is a detectable doc/KB platform** — none was bespoke. They sort into 5 hierarchy-acquisition strategies:

| Platform | Targets | Strategy |
|---|---|---|
| Commvault (custom) | *(existing)* | sidebar-tree |
| Docusaurus | Portworx | sidebar-tree |
| MkDocs | Satori | sidebar-tree |
| GitBook | Trilio | sidebar-tree (SPA) |
| MadCap Flare HTML5 | Datto M365, Datto SIRIS | sidebar-tree |
| MadCap Flare WebHelp | Acronis, Arcserve | frames / per-topic files |
| Intercom help center | Druva, Gearset | hub-and-spoke crawl |
| Freshdesk help center | Keepit | hub-and-spoke crawl |
| Confluence | Barracuda | sidebar-tree (JS-gated) |
| *(unknown)* | future | generic (sitemap) / LLM fallback |

## Decisions (locked during brainstorming)

| Decision | Choice |
|---|---|
| Ingestion | **Uniform Firecrawl scraping + per-platform profiles** (no native-API path now; APIs are a documented escape hatch if a platform's DOM proves too fragile). |
| Selection | **Auto-detect by fingerprint, stored on the source, with a UI override** (known platforms + Auto + Generic). User override always wins. |
| Scope | **Everything in one spec** — foundation + all surveyed platform profiles + hub-and-spoke crawl + generic + LLM fallback. Implementation is decomposed into one independent task per profile. |
| Intelligence | Mostly **deterministic platform detection + profile registry**; an **LLM fallback** derives a profile only for unrecognized sites (one-shot per source, cached). |
| Testing | **Saved HTML fixtures** per platform → offline unit tests of `detect()` + `build_toc()`; plus per-platform live smoke checks. |

## Architecture

### Profile interface

A profile is a small unit with one clear responsibility — turn a source into an ordered TOC and tell the scraper which DOM is content. New package `app/services/profiles/`:

```python
class TocEntry:  # plain dataclass
    title: str; url: str; level: int; is_article: bool; parent_url: str | None

class ExtractionProfile(Protocol):
    name: str                                  # stored in DocumentationSource.platform
    def detect(self, root_html: str, root_url: str) -> bool: ...
    async def build_toc(self, root_url: str, scraper) -> list[TocEntry]: ...   # ORDERED
    def content_config(self, url: str) -> dict: ...   # {"includeTags": [...]} | {"onlyMainContent": True, "waitFor": ms}
```

`scraper` is a thin adapter over `FirecrawlService` (scrape a URL → html; map URLs; respect rate limits) so profiles don't import the service directly and are unit-testable with a fake scraper.

### Integration into the existing pipeline

`FirecrawlService.extract_source` is refactored:
1. Resolve the source's profile (stored `platform`, else detect, else generic/LLM).
2. **Phase 1 (TOC):** `entries = await profile.build_toc(root_url, scraper)` → persisted exactly as today (delete+rebuild `TOCEntry`, ordered, with parent links). The current Commvault `_build_toc_recursive`/`_parse_nav_items` move into the Commvault profile unchanged.
3. **Phase 2 (content):** for each page, the batch/scrape call uses `profile.content_config(url)` instead of the hardcoded `includeTags:["#doc"]`. The `<time>`/last-updated parse and image handling stay (they already operate on the returned content HTML).

Everything downstream — worker queue, changeTracking, incremental, removed-page detection, the image pipeline, the bounded export — is **unchanged**.

### Detection & selection

- `app/services/profiles/detector.py`: runs each registered profile's `detect()` against the rendered root HTML; returns the first/highest-confidence match, else `None`.
- `DocumentationSource` gains `platform: str | None` (the chosen profile name; `null` = not yet detected) and `profile_config: JSONB | None` (overrides + LLM-derived selectors).
- On extraction, if `platform` is unset: detect, persist the result. UI: a dropdown on the source (known platforms + **Auto** + **Generic**); setting it writes `platform`. Override always wins over detection.

## Profile catalog

Each profile encodes its TOC strategy, content selector, and any JS/frame handling. Selectors below are the design intent; exact values are finalized against fixtures during implementation.

- **Commvault** — sidebar-tree; `#nav .nav-group` recursive (existing logic); content `#doc`.
- **Docusaurus** — sidebar-tree; nested `.theme-doc-sidebar-menu` (`-item-category` = section, `-item-link` = article); content `.theme-doc-markdown` / `<article>`.
- **MkDocs** — sidebar-tree; `.md-nav` nested lists; content `<main>` / `<article>`.
- **GitBook** — sidebar-tree (SPA — `waitFor`); sidebar nav links; content `<main>`.
- **MadCap Flare HTML5** — sidebar-tree; `.sidenav` tree (Flare lazy-loads child TOC chunks — follow them or expand via `waitFor`); content `[data-mc-content-body]`.
- **MadCap Flare WebHelp** — frames/topic-files; the index is a frameset (`<iframe id="topic">`) so the TOC comes from Flare's TOC data (e.g. `Data/Tocs/*` or the `toc` tree) and content is scraped from each **topic page URL directly** (never the frameset); content = topic body.
- **Intercom help center** — hub-and-spoke crawl; from root follow `.collection-link` (collections) → section pages → article links; preserve traversal order as hierarchy; content `<article>` / main.
- **Freshdesk help center** — hub-and-spoke crawl; `/support/solutions` category pages → folders → `article-list` article links; content `<article>` / main.
- **Confluence** — sidebar-tree, JS-gated (highest risk); render with `waitFor` and read the space page-tree; content `.wiki-content`. If the rendered tree proves unreliable, the REST API (`/rest/api/content`) is the documented follow-on escape hatch.
- **Generic (fallback)** — enumerate via `sitemap.xml` (or Firecrawl `/map`); reconstruct best-effort hierarchy from URL path segments; content `onlyMainContent`. Ordering is best-effort (sitemaps are flat) — the one case where exact TOC order isn't guaranteed.
- **LLM (fallback)** — for an unrecognized, structured site: an LLM inspects the rendered root (+ one child page) and returns `{strategy, nav_selector, item_selectors, content_selector, parent_rule}`, cached in `profile_config`; subsequent runs apply it deterministically. Tested with a mocked LLM.

## Ordering guarantee

Sidebar-tree, frame/topic, and hub-and-spoke strategies preserve source order from DOM/crawl order — the tool's "preserve original TOC order" promise holds for every recognized platform. Only the **generic sitemap fallback** is best-effort.

## Testing

- **Fixtures:** capture each platform's real root HTML into `backend/tests/fixtures/platforms/<platform>.html` (via Firecrawl during the foundation task).
- **Unit (offline, deterministic):** per profile, `test_detect_<platform>` (its fixture matches its profile and not others) and `test_build_toc_<platform>` (parsing the fixture yields a sane ordered hierarchy — expected top-level titles/order, nesting). The fake `scraper` returns fixture HTML for given URLs so `build_toc` runs without network.
- **Detector:** each fixture resolves to its platform; an unknown fixture resolves to `None`.
- **LLM fallback:** mocked LLM returns a fixed `profile_config`; assert it drives a deterministic `build_toc`.
- **Existing extraction tests:** unchanged behavior for the Commvault path (now via its profile).
- **Live smoke (verification):** per platform, scrape the real root and assert a non-empty ordered TOC + non-empty content on a sample article (Confluence and Flare-WebHelp are the ones most likely to need iteration).

## Out of scope / risks
- Native platform APIs (Confluence/Intercom/Freshdesk/Zendesk) — deferred escape hatch, not built now.
- Confluence pure-scrape is the highest-risk profile (heavy JS); may need Firecrawl actions/waits and could land as "best-effort" pending the API follow-on.
- The LLM fallback adds a model dependency + cost; it is a safety net, not the common path.
- Per-locale / multi-language handling, auth-gated docs, and PDF-only docs are not addressed.
