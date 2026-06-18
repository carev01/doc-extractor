# Generalized Extraction via Platform Profiles — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the hardcoded Commvault TOC/content extraction with a pluggable, auto-detected **platform-profile** architecture covering all surveyed platforms, with generic-sitemap and LLM fallbacks.

**Architecture:** A `profiles` package defines an `ExtractionProfile` interface (`detect`, `build_toc`, `content_config`) and shared strategy helpers (`sidebar_tree_toc`, `hubspoke_toc`, sitemap enumeration). `FirecrawlService.extract_source` resolves a source's profile (stored → detected → generic), calls `build_toc` for Phase 1, and uses `content_config` for Phase 2 scraping. Profiles are unit-tested offline against saved HTML fixtures via a fake scraper.

**Tech Stack:** FastAPI, SQLAlchemy async, Alembic, BeautifulSoup4, Firecrawl; React/TS frontend. Profiles are pure-ish Python + a thin scraper adapter.

## Global Constraints

- New Alembic migration `down_revision = f5a6b7c8d9e0` (current head).
- The hardcoded `includeTags: ["#doc"]` appears in **both** `_scrape_article` and `_submit_batch` — both must take their content options from the resolved profile's `content_config()`.
- **Uniform pipeline:** profiles only change (a) how the ordered TOC is built and (b) the per-source content scrape options. The worker queue, changeTracking, image pipeline, incremental, removed-page detection, and bounded export are untouched.
- Profiles return **ordered** `TocEntry` lists; the persisted `TOCEntry` rows keep depth-first order + parent links exactly as today.
- Profiles never import `FirecrawlService` directly — they take a `scraper` adapter, so they unit-test with a `FakeScraper` returning fixture HTML (no network).
- Fixtures live in `backend/tests/fixtures/platforms/<name>.html`; profile unit tests are offline/deterministic.
- Firecrawl for fixture capture / live checks: `http://firecrawl.k3s.home.lan`, bearer `fc-bf48f20724d6459cbdda97aef48a41fb`, `POST /v2/scrape {"url","formats":["html"],"onlyMainContent":false}`.
- Run backend tests from `backend/` with `pytest`; frontend via `npm run build` + `npm run lint`.
- Branch `feat/extraction-profiles` (off `main`). Interpreter `python3`.

---

### Task 1: Source `platform` + `profile_config` columns

**Files:**
- Modify: `backend/app/models/source.py`
- Create: `backend/alembic/versions/a1b2c3d4e5f7_add_source_platform.py`
- Modify: `backend/app/schemas/source.py`, `backend/app/routes/sources.py`

- [ ] **Step 1: Add columns to the model**

In `backend/app/models/source.py`, after `error_message`:
```python
    # Extraction platform profile (e.g. "commvault", "docusaurus", "intercom").
    # NULL = not yet detected; "generic" = sitemap fallback. Set by detection or UI override.
    platform: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Optional per-source overrides / LLM-derived selectors for the profile.
    profile_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
```
Add `from sqlalchemy.dialects.postgresql import JSONB` to the imports (keep the existing `UUID` import line).

- [ ] **Step 2: Migration**

Create `backend/alembic/versions/a1b2c3d4e5f7_add_source_platform.py`:
```python
"""add source platform + profile_config

Revision ID: a1b2c3d4e5f7
Revises: f5a6b7c8d9e0
"""
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "a1b2c3d4e5f7"
down_revision: Union[str, Sequence[str], None] = "f5a6b7c8d9e0"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.add_column("documentation_sources", sa.Column("platform", sa.String(64), nullable=True))
    op.add_column("documentation_sources", sa.Column("profile_config", JSONB, nullable=True))

def downgrade() -> None:
    op.drop_column("documentation_sources", "profile_config")
    op.drop_column("documentation_sources", "platform")
```
Apply: `cd backend && alembic upgrade head` → expect head `a1b2c3d4e5f7`.

- [ ] **Step 3: Expose in schema + allow override**

In `backend/app/schemas/source.py`: add `platform: str | None = None` to `SourceResponse`; add `platform: str | None = None` to `SourceUpdate` (so the UI can override). In `backend/app/routes/sources.py`, the existing source-update route must persist `platform` when provided (apply it like `name`/`base_url`).

- [ ] **Step 4: Run + commit**

Run: `cd backend && pytest -q` (existing tests pass; new columns are nullable).
```bash
git add backend/app/models/source.py backend/alembic/versions/a1b2c3d4e5f7_add_source_platform.py backend/app/schemas/source.py backend/app/routes/sources.py
git commit -m "feat(db): add source.platform + profile_config for extraction profiles"
```

---

### Task 2: Profile interface, scraper adapter, registry, shared strategy helpers

**Files:**
- Create: `backend/app/services/profiles/__init__.py`, `base.py`, `scraper.py`, `strategies.py`, `registry.py`
- Test: `backend/tests/test_profile_strategies.py`

**Interfaces (Produces):**
- `TocEntry(title: str, url: str, level: int, is_article: bool, parent_url: str | None)` (dataclass).
- `class ExtractionProfile(Protocol)`: `name: str`; `detect(root_html: str, root_url: str) -> bool`; `async build_toc(root_url, scraper) -> list[TocEntry]`; `content_config() -> dict`.
- `class Scraper`: `async get_html(url, wait_ms=1500) -> str`; `async map_urls(root_url) -> list[str]`. Plus `FakeScraper(html_by_url: dict)` for tests.
- `async sidebar_tree_toc(scraper, root_url, nav_selector, *, item_selector="a", section_predicate=None, wait_ms=1500) -> list[TocEntry]` — scrape root, parse the nav container's nested `<ul>/<li>` into ordered TocEntry with levels/parents.
- `async hubspoke_toc(scraper, root_url, *, category_link_selector, article_link_selector, section_link_selector=None) -> list[TocEntry]` — crawl root→categories→(sections)→articles, preserving order.
- `async sitemap_urls(scraper, root_url) -> list[str]` — fetch `sitemap.xml`, return URLs in document order.

- [ ] **Step 1: Write the failing tests** (`backend/tests/test_profile_strategies.py`)

```python
import os, sys
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.services.profiles.base import TocEntry
from app.services.profiles.scraper import FakeScraper
from app.services.profiles.strategies import sidebar_tree_toc, hubspoke_toc

NEST = """<html><body><nav id="t">
<ul><li><a href="/a">A</a><ul>
  <li><a href="/a/1">A1</a></li><li><a href="/a/2">A2</a></li></ul></li>
<li><a href="/b">B</a></li></ul></nav></body></html>"""

@pytest.mark.asyncio
async def test_sidebar_tree_levels_and_order():
    sc = FakeScraper({"https://x/": NEST})
    toc = await sidebar_tree_toc(sc, "https://x/", "#t")
    assert [(e.title, e.level) for e in toc] == [("A",0),("A1",1),("A2",1),("B",0)]
    assert toc[1].parent_url.endswith("/a")

HUB = {
  "https://x/": '<a class="cat" href="https://x/c1">Cat1</a><a class="cat" href="https://x/c2">Cat2</a>',
  "https://x/c1": '<a class="art" href="https://x/c1/a">C1A</a><a class="art" href="https://x/c1/b">C1B</a>',
  "https://x/c2": '<a class="art" href="https://x/c2/a">C2A</a>',
}
@pytest.mark.asyncio
async def test_hubspoke_order_and_hierarchy():
    toc = await hubspoke_toc(FakeScraper(HUB), "https://x/",
        category_link_selector="a.cat", article_link_selector="a.art")
    assert [(e.title, e.level, e.is_article) for e in toc] == [
        ("Cat1",0,False),("C1A",1,True),("C1B",1,True),("Cat2",0,False),("C2A",1,True)]
```

- [ ] **Step 2: Run → FAIL** (`pytest tests/test_profile_strategies.py -v` → ModuleNotFoundError).

- [ ] **Step 3: Implement the package**

`base.py`:
```python
from dataclasses import dataclass
from typing import Protocol

@dataclass
class TocEntry:
    title: str
    url: str
    level: int
    is_article: bool = True
    parent_url: str | None = None

class ExtractionProfile(Protocol):
    name: str
    def detect(self, root_html: str, root_url: str) -> bool: ...
    async def build_toc(self, root_url: str, scraper) -> list["TocEntry"]: ...
    def content_config(self) -> dict: ...
```

`scraper.py`:
```python
from urllib.parse import urljoin

class Scraper:
    """Thin adapter over FirecrawlService for profiles (so profiles stay testable)."""
    def __init__(self, firecrawl):
        self._fc = firecrawl
    async def get_html(self, url: str, wait_ms: int = 1500) -> str:
        data = await self._fc._firecrawl_request(url, {
            "formats": ["html"], "onlyMainContent": False, "waitFor": wait_ms,
        })
        return data.get("html", "")
    async def map_urls(self, root_url: str) -> list[str]:
        return await self._fc.map_urls(root_url)  # added in Task 14

class FakeScraper:
    def __init__(self, html_by_url: dict[str, str], urls: list[str] | None = None):
        self._h = html_by_url; self._urls = urls or list(html_by_url)
    async def get_html(self, url: str, wait_ms: int = 1500) -> str:
        return self._h.get(url, "")
    async def map_urls(self, root_url: str) -> list[str]:
        return list(self._urls)
```

`strategies.py` (uses BeautifulSoup; `urljoin` for absolute URLs):
```python
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from .base import TocEntry

async def sidebar_tree_toc(scraper, root_url, nav_selector, *, item_selector="a", wait_ms=1500):
    soup = BeautifulSoup(await scraper.get_html(root_url, wait_ms), "html.parser")
    nav = soup.select_one(nav_selector)
    out: list[TocEntry] = []
    if not nav:
        return out
    def walk(ul, level, parent_url):
        for li in ul.find_all("li", recursive=False):
            a = li.find(item_selector, recursive=True)
            if not a or not a.get("href"):
                # section without its own link: descend using its text as a section node
                continue
            url = urljoin(root_url, a["href"])
            child_ul = li.find("ul", recursive=False)
            out.append(TocEntry(title=a.get_text(strip=True), url=url, level=level,
                                is_article=child_ul is None, parent_url=parent_url))
            if child_ul:
                walk(child_ul, level + 1, url)
    top = nav.find("ul")
    if top:
        walk(top, 0, None)
    return out

async def hubspoke_toc(scraper, root_url, *, category_link_selector, article_link_selector,
                       section_link_selector=None):
    root = BeautifulSoup(await scraper.get_html(root_url), "html.parser")
    out: list[TocEntry] = []
    for cat in root.select(category_link_selector):
        if not cat.get("href"):
            continue
        cat_url = urljoin(root_url, cat["href"])
        out.append(TocEntry(cat.get_text(strip=True), cat_url, 0, False, None))
        cat_soup = BeautifulSoup(await scraper.get_html(cat_url), "html.parser")
        containers = ([(s, urljoin(cat_url, s["href"])) for s in cat_soup.select(section_link_selector) if s.get("href")]
                      if section_link_selector else [(None, cat_url)])
        for sec_el, sec_url in containers:
            sec_soup = cat_soup if sec_el is None else BeautifulSoup(await scraper.get_html(sec_url), "html.parser")
            sec_parent = cat_url
            sec_level = 1
            if sec_el is not None:
                out.append(TocEntry(sec_el.get_text(strip=True), sec_url, 1, False, cat_url))
                sec_parent, sec_level = sec_url, 2
            for art in sec_soup.select(article_link_selector):
                if art.get("href"):
                    out.append(TocEntry(art.get_text(strip=True), urljoin(sec_url, art["href"]),
                                        sec_level, True, sec_parent))
    return out

async def sitemap_urls(scraper, root_url):
    from urllib.parse import urlparse
    base = f"{urlparse(root_url).scheme}://{urlparse(root_url).netloc}"
    xml = await scraper.get_html(urljoin(base + "/", "sitemap.xml"))
    soup = BeautifulSoup(xml, "xml")
    return [loc.get_text(strip=True) for loc in soup.find_all("loc")]
```

`registry.py`:
```python
PROFILES: list = []  # populated by each profile module's register() at import

def register(profile) -> None:
    PROFILES.append(profile)

def get(name: str):
    return next((p for p in PROFILES if p.name == name), None)
```
`__init__.py` imports all profile modules so they self-register (extended as profiles are added).

- [ ] **Step 4: Run → PASS** (`pytest tests/test_profile_strategies.py -v`).

- [ ] **Step 5: Commit**
```bash
git add backend/app/services/profiles backend/tests/test_profile_strategies.py
git commit -m "feat(profiles): profile interface, scraper adapter, registry, strategy helpers"
```

---

### Task 3: Commvault profile (extract existing logic)

**Files:** Create `backend/app/services/profiles/commvault.py`; modify `backend/app/services/firecrawl.py` (move TOC code); add fixture `backend/tests/fixtures/platforms/commvault.html`; test `backend/tests/test_profiles_commvault.py`.

- [ ] **Step 1: Capture a Commvault fixture**
Scrape the Clumio root via Firecrawl (see Global Constraints) and save the HTML to `backend/tests/fixtures/platforms/commvault.html`.

- [ ] **Step 2: Move the Commvault TOC logic into the profile**
Create `commvault.py` with a `CommvaultProfile` whose `build_toc` contains the current `_build_toc_recursive` + `_parse_nav_items` + `_scrape_nav_html` logic (moved verbatim, adapted to use the `scraper` adapter and return `TocEntry` instead of dicts), `detect()` matches `id="nav"` + `class="nav-group"`, and `content_config()` returns `{"includeTags": ["#doc"], "onlyMainContent": False, "waitFor": 1500}`. Register it. Delete those methods from `firecrawl.py` (Task 4 wires the profile in).

- [ ] **Step 3: Test against the fixture**
`test_profiles_commvault.py`: load the fixture, assert `CommvaultProfile().detect(html, url) is True`; with a `FakeScraper` serving the fixture as the nav HTML, assert `build_toc` yields a non-empty ordered list whose first entries match the known Clumio top-level titles; assert `content_config()["includeTags"] == ["#doc"]`.

- [ ] **Step 4: Commit** `git commit -m "feat(profiles): Commvault profile (extracted from FirecrawlService)"`

---

### Task 4: Wire profile resolution into the extraction pipeline

**Files:** Modify `backend/app/services/firecrawl.py`; modify `backend/tests/test_versions.py`/`test_integration.py` as needed.

- [ ] **Step 1:** Add a resolver: `_resolve_profile(source, root_html) -> ExtractionProfile` — if `source.platform` set, `registry.get(source.platform)`; else run the detector (Task 5) and persist the result; else the generic profile (Task 14). Until Tasks 5/14 land, default to `CommvaultProfile` so existing behavior holds.
- [ ] **Step 2:** In `extract_source`, replace the direct `_build_toc_recursive(...)` call with `profile = self._resolve_profile(source, root_html); entries = await profile.build_toc(base_url, Scraper(self))`; map `TocEntry → the existing dict shape` used by the persistence code (title/url/level/is_article/parent).
- [ ] **Step 3:** In `_scrape_article` and `_submit_batch`, replace the hardcoded `"includeTags": ["#doc"]` / `"onlyMainContent": False` with the resolved profile's `content_config()` (thread the profile/config into both; `_submit_batch` applies one config to the batch).
- [ ] **Step 4:** Run the full suite (`pytest -q`); the Commvault/extraction tests must still pass (behavior identical via the profile). Commit `git commit -m "refactor(extraction): drive TOC + content scraping through the resolved profile"`.

---

### Task 5: Fixtures + platform detector

**Files:** Create `backend/app/services/profiles/detector.py`; capture all fixtures under `backend/tests/fixtures/platforms/`; test `backend/tests/test_detector.py`.

- [ ] **Step 1:** Capture each platform root via Firecrawl into `backend/tests/fixtures/platforms/{docusaurus,mkdocs,gitbook,flare_html5,flare_webhelp,intercom,freshdesk,confluence}.html` (URLs from the spec survey).
- [ ] **Step 2:** `detector.py`: `detect_platform(root_html, root_url) -> str | None` iterates `registry.PROFILES`, returns the first profile whose `detect()` is True, else None.
- [ ] **Step 3:** `test_detector.py`: each fixture resolves to its own platform name and NOT to another's; a junk HTML returns None. (This test grows as profiles are added; assert the subset present so far.) Commit.

---

### Tasks 6–13: One profile per platform

Each task: create `backend/app/services/profiles/<name>.py` (register it), capture/confirm its fixture, and add `backend/tests/test_profiles_<name>.py` with `test_detect` (fixture matches this profile, not others) + `test_build_toc` (FakeScraper over fixture(s) → expected ordered titles/levels). Concrete per-profile config:

- [ ] **Task 6 — Docusaurus** (`docusaurus.py`): `detect` = `theme-doc-sidebar-menu` present. `build_toc` = `sidebar_tree_toc(scraper, root, ".theme-doc-sidebar-menu")`. `content_config` = `{"includeTags": [".theme-doc-markdown"], "onlyMainContent": False, "waitFor": 1500}`. Fixture: Portworx. Commit.
- [ ] **Task 7 — MkDocs** (`mkdocs.py`): `detect` = `md-nav` / `data-md-component` present. `build_toc` = `sidebar_tree_toc(scraper, root, ".md-nav--primary > .md-nav__list", )`. `content_config` = `{"includeTags": ["article.md-content__inner"], "onlyMainContent": False}`. Fixture: Satori. Commit.
- [ ] **Task 8 — GitBook** (`gitbook.py`): `detect` = GitBook fingerprint (`content="GitBook"` / `__GITBOOK`). `build_toc` = `sidebar_tree_toc(scraper, root, "nav[aria-label]", wait_ms=3000)` (SPA — longer wait). `content_config` = `{"onlyMainContent": True, "waitFor": 3000}`. Fixture: Trilio. Commit.
- [ ] **Task 9 — MadCap Flare HTML5** (`flare_html5.py`): `detect` = `MadCap` + `sidenav`, no frameset. `build_toc` = `sidebar_tree_toc(scraper, root, ".sidenav", wait_ms=2000)` (if child TOC chunks lazy-load, follow `data-mc-*` chunk links — implement a Flare-specific descent if the fixture shows chunked TOC). `content_config` = `{"includeTags": ["[data-mc-content-body]"], "onlyMainContent": False, "waitFor": 1500}`. Fixtures: Datto M365 + SIRIS. Commit.
- [ ] **Task 10 — MadCap Flare WebHelp (frames)** (`flare_webhelp.py`): `detect` = `MadCap` + `<iframe id="topic"`. `build_toc` = read Flare's TOC data (the `Data/Tocs/*.js`/`*.xml` referenced by the help system, or the TOC tree the frameset loads) → topic URLs under `/Content/...`; **never** treat the frameset as content. If the TOC data file is JSON/XML, parse it into ordered entries; else enumerate via `sitemap_urls`. `content_config` = topic body selector (e.g. `{"includeTags": ["[data-mc-content-body]"], "onlyMainContent": False}` or `{"onlyMainContent": True}`). Fixtures: Acronis + Arcserve (+ a captured TOC data file). This is the highest-risk profile — if the TOC data format resists parsing, fall back to `sitemap_urls` + URL-path hierarchy and note it. Commit.
- [ ] **Task 11 — Intercom help center** (`intercom.py`): `detect` = `collection-link` + `collection-summary` classes. `build_toc` = `hubspoke_toc(scraper, root, category_link_selector="a.collection-link", section_link_selector="a.collection-link", article_link_selector="a[href*='/articles/']")` (Intercom nests collections→collections→articles; if only 2 levels appear in the fixture, omit `section_link_selector`). `content_config` = `{"onlyMainContent": True, "waitFor": 1500}`. Fixtures: Druva + Gearset. Commit.
- [ ] **Task 12 — Freshdesk help center** (`freshdesk.py`): `detect` = `/support/solutions` links or Freshworks fingerprint. `build_toc` = `hubspoke_toc(scraper, root, category_link_selector="a[href*='/support/solutions/']", section_link_selector="a[href*='/support/solutions/folders/']", article_link_selector="a[href*='/support/solutions/articles/']")`. `content_config` = `{"onlyMainContent": True}`. Fixture: Keepit. Commit.
- [ ] **Task 13 — Confluence** (`confluence.py`): `detect` = Atlassian/Confluence fingerprint. `build_toc` = render the space overview with a long `waitFor` (e.g. 4000) and parse the page-tree links; if the initial HTML is a stub (as in the survey), this profile may yield little without JS actions — implement best-effort and clearly mark the limitation. `content_config` = `{"includeTags": [".wiki-content"], "onlyMainContent": False, "waitFor": 4000}`. Fixture: Barracuda. Note in the report if the rendered tree is insufficient (the REST API is the documented follow-on). Commit.

---

### Task 14: Generic sitemap fallback profile

**Files:** Create `backend/app/services/profiles/generic.py`; add `FirecrawlService.map_urls`; test `backend/tests/test_profiles_generic.py`.

- [ ] **Step 1:** Add `async def map_urls(self, root_url) -> list[str]` to `FirecrawlService` (POST `/v2/map` if available, else fetch `sitemap.xml`).
- [ ] **Step 2:** `GenericProfile`: `name="generic"`, `detect` always returns False (never auto-selected — only chosen as explicit fallback/override). `build_toc` = `sitemap_urls(scraper, root)` filtered to under `root_url`'s path; build best-effort hierarchy from URL path depth (level = number of path segments below root; parent = the URL one segment up if present); `is_article=True` for leaves. `content_config` = `{"onlyMainContent": True, "waitFor": 1500}`.
- [ ] **Step 3:** Test with a `FakeScraper` whose `sitemap.xml` lists nested URLs → assert levels/parents derived from path depth, document order preserved. Wire `_resolve_profile` to use `GenericProfile` when detection returns None. Commit.

---

### Task 15: LLM fallback profile

**Files:** Create `backend/app/services/profiles/llm.py`; modify `_resolve_profile`; test `backend/tests/test_profiles_llm.py`. Adds an LLM client dependency (reuse the project's configured model access; gate behind a settings flag `DOCEXTRACTOR_LLM_FALLBACK_ENABLED`, default off).

- [ ] **Step 1:** `LlmProfile`: given a source with no detected platform and `DOCEXTRACTOR_LLM_FALLBACK_ENABLED`, on first `build_toc` it sends the rendered root (+1 child) HTML (truncated) to the LLM asking for `{strategy, nav_selector, item_selector, content_selector, category_link_selector, article_link_selector}`; stores the result in `source.profile_config`; then dispatches to `sidebar_tree_toc`/`hubspoke_toc`/`sitemap_urls` per `strategy` using the returned selectors. `content_config()` returns the derived content selector.
- [ ] **Step 2:** Test with a **mocked** LLM returning a fixed config (sidebar strategy + `#t` selector) against the nested fixture from Task 2 → assert it produces the same TOC as `sidebar_tree_toc`. Assert it's a no-op (returns []/raises a clear skip) when the flag is off.
- [ ] **Step 3:** `_resolve_profile` order becomes: stored `platform` → detector → (LLM if enabled) → generic. Commit.

---

### Task 16: Frontend platform override

**Files:** Modify `frontend/src/types/index.ts`, `frontend/src/api/client.ts` (source update), `frontend/src/components/SourceList.tsx` (or wherever a source is edited).

- [ ] **Step 1:** Add `platform?: string | null` to the `DocumentationSource` type.
- [ ] **Step 2:** On the source row/detail, show the detected platform and a small dropdown to override it (options: the known profile names + `auto` (sends null) + `generic`), calling the existing source-update endpoint with `{platform}`.
- [ ] **Step 3:** `npm run build` + `npm run lint` clean (no new errors). Commit.

---

### Task 17: Live smoke verification

**Files:** none.

- [ ] **Step 1:** Rebuild: `docker compose up -d --build backend worker frontend`.
- [ ] **Step 2:** For each platform, add a source pointing at its survey root, trigger extraction, and confirm: detection set the right `platform`; the TOC came out ordered + non-empty with sane nesting; a sample article's content is non-empty and on-topic (not nav chrome). Record per-platform results. Flag Confluence and Flare-WebHelp explicitly (most likely to need iteration).
- [ ] **Step 3:** Confirm the existing Commvault/Clumio source still extracts identically (regression).

---

## Self-Review

**Spec coverage:** profile interface + integration → Tasks 2,4; source field + UI override → Tasks 1,16; detector + fixtures → Task 5; all surveyed platforms → Tasks 3,6–13; generic + LLM fallbacks → Tasks 14,15; ordering preserved by `sidebar_tree_toc`/`hubspoke_toc` (DOM/crawl order); fixture-based offline tests → every profile task; live smoke → Task 17.

**Placeholder scan:** No TBD. Selector values per profile are concrete (from the survey fingerprints); where a platform's exact TOC-data format can't be known until a fixture is in hand (Flare-WebHelp TOC files, Flare-HTML5 lazy chunks, Confluence JS tree), the task names the primary approach AND the documented fallback (`sitemap_urls`/best-effort), which is a real instruction, not a placeholder. These three are the flagged-risk profiles.

**Type consistency:** `TocEntry` and the `ExtractionProfile` methods are used identically across foundation, profiles, and the resolver; `Scraper`/`FakeScraper` share `get_html`/`map_urls`; `content_config()` returns the scrape-options dict consumed by both `_scrape_article` and `_submit_batch`; `_resolve_profile` order (stored → detect → LLM → generic) is consistent across Tasks 4/14/15.

## Out of scope / risks (from spec)
Native platform APIs (deferred escape hatch); Confluence pure-scrape may be best-effort; LLM fallback is a gated safety net; multi-language/auth-gated/PDF-only docs not addressed.
