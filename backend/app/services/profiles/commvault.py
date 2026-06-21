"""Commvault documentation profile.

Two TOC modes, by what the source is rooted at:

* FULL (rooted at ``index.html``) — the whole product doc set. ``nav-map.json``
  lists every page (flat); each page's ``nav-path`` meta is its ancestor chain,
  so we fetch the pages and reconstruct the full hierarchical tree. This is a
  large set (thousands of pages).
* SECTION (rooted at a specific page) — just that bookshelf. The sidebar nav is
  rendered client-side into ``#nav`` ("Loading…" in static HTML), so we render
  it via Browserless and walk the active section's ``<ul>``/``<li.nav-row>`` tree.

Article content is server-rendered in ``#doc``, so content scraping uses the
normal Firecrawl path (no per-article browser render).
"""

import asyncio
import json
import logging
import re
from html import unescape
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from app.services.profiles import registry
from app.services.profiles.base import TocEntry

logger = logging.getLogger(__name__)

# Max concurrent raw fetches when reconstructing the full-doc hierarchy.
_FULL_TOC_CONCURRENCY = 12


class CommvaultProfile:
    name = "commvault"

    def detect(self, root_html: str, root_url: str) -> bool:
        # New platform: documentation.commvault.com (nav is "Loading…" client-side,
        # so key off the host / cv- markers). Old platform: inline #nav + nav-group.
        host = urlparse(root_url).netloc
        if host.endswith("documentation.commvault.com") or "cv-nav-slug" in root_html:
            return True
        return 'id="nav"' in root_html and "nav-group" in root_html

    def content_config(self) -> dict:
        # #doc holds the article, but starts with a ">" breadcrumb trail; drop it.
        return {
            "includeTags": ["#doc"],
            "excludeTags": [".breadcrumbs"],
            "onlyMainContent": False,
            "waitFor": 1500,
        }

    @staticmethod
    def _row_anchor(li):
        """The row's own <a> (inside its .nav-item label, not a nested child)."""
        item = li.find(class_="nav-item")
        a = item.find("a") if item else li.find("a")
        return a

    @staticmethod
    def _child_rows(li):
        """Direct child nav-rows (the row's own nested <ul>)."""
        child_ul = li.find("ul")
        if not child_ul:
            return []
        return child_ul.find_all("li", class_="nav-row", recursive=False)

    def _walk(self, li, level, parent_url, root_url, out):
        a = self._row_anchor(li)
        href = (a.get("href") or "").strip() if a else ""
        title = a.get_text(strip=True) if a else ""
        if not href or not title:
            return
        url = urljoin(root_url, href)
        children = self._child_rows(li)
        out.append(TocEntry(
            title=title, url=url, level=level,
            is_article=not children, parent_url=parent_url,
        ))
        for child in children:
            self._walk(child, level + 1, url, root_url, out)

    async def build_toc(self, root_url: str, scraper) -> list[TocEntry]:
        """Dispatch: full doc set when rooted at index.html, else the section."""
        if urlparse(root_url).path.rsplit("/", 1)[-1] in ("index.html", "index.htm", ""):
            return await self._build_full_toc(root_url, scraper)
        return await self._build_section_toc(root_url, scraper)

    # ── FULL mode: whole doc set from nav-map.json + per-page nav-path ─────────

    async def _build_full_toc(self, root_url: str, scraper) -> list[TocEntry]:
        """Build the entire hierarchical TOC for the product docs.

        ``nav-map.json`` lists every page (flat); each page's ``nav-path`` meta is
        its ancestor key-chain. We fetch the pages (bounded concurrency) to read
        nav-path + title, then emit a DFS pre-order tree (parents before children).
        """
        base = root_url.rsplit("/", 1)[0] + "/"
        try:
            files = json.loads(await scraper.get_raw(base + "static/scripts/nav-map.json"))
        except Exception as exc:
            logger.warning("Commvault nav-map.json fetch failed: %s", exc)
            return []
        files = [f for f in files if isinstance(f, str) and f.endswith(".html")]
        if not files:
            return []

        # key (filename without .html) -> {title, navpath, order}
        pages: dict[str, dict] = {}
        sem = asyncio.Semaphore(_FULL_TOC_CONCURRENCY)
        nav_path_re = re.compile(r'<meta name="nav-path" content="([^"]*)"')
        h1_re = re.compile(r'<h1[^>]*class="heading"[^>]*>(.*?)</h1>', re.S)

        async def fetch(order: int, fname: str) -> None:
            key = fname[:-5]
            title = key.replace("_", " ").strip().capitalize()
            navpath = [key]
            async with sem:
                try:
                    html = await scraper.get_raw(base + fname)
                except Exception:
                    html = ""
            if html:
                m = nav_path_re.search(html)
                if m:
                    try:
                        parsed = json.loads(unescape(m.group(1)))
                        if isinstance(parsed, list) and parsed:
                            navpath = [str(x) for x in parsed]
                    except (ValueError, TypeError):
                        pass
                hm = h1_re.search(html)
                if hm:
                    title = unescape(re.sub(r"<[^>]+>", "", hm.group(1))).strip() or title
            pages[key] = {"title": title, "navpath": navpath, "order": order}

        await asyncio.gather(*(fetch(i, f) for i, f in enumerate(files)))

        # Group children by parent key (navpath[-2]); preserve nav-map order.
        children: dict[str | None, list[str]] = {}
        for key, p in sorted(pages.items(), key=lambda kv: kv[1]["order"]):
            parent = p["navpath"][-2] if len(p["navpath"]) >= 2 else None
            children.setdefault(parent if parent in pages else None, []).append(key)

        out: list[TocEntry] = []

        def walk(parent_key: str | None, level: int, parent_url: str | None) -> None:
            for key in children.get(parent_key, []):
                p = pages[key]
                url = base + key + ".html"
                kids = children.get(key)
                out.append(TocEntry(
                    title=p["title"], url=url, level=level,
                    is_article=True, parent_url=parent_url,
                ))
                if kids:
                    walk(key, level + 1, url)

        walk(None, 0, None)
        return out

    # ── SECTION mode: active bookshelf via the Browserless-rendered nav ────────

    async def _build_section_toc(self, root_url: str, scraper) -> list[TocEntry]:
        """Render the sidebar via Browserless and return the active section's tree.

        Scopes to the bookshelf the user rooted at: the nav-row whose link is the
        requested page (the active/open section), plus its nested descendants.
        """
        html = await scraper.get_rendered_html(root_url, wait_for=".nav-row")
        if not html:
            return []
        soup = BeautifulSoup(html, "html.parser")
        nav = soup.find(id="nav")
        if not nav:
            return []

        rows = nav.find_all("li", class_="nav-row")
        if not rows:
            return []

        # Find the section to scope to: the row whose link matches the requested
        # page (by filename), else the open/active row, else the first top-level row.
        root_file = urlparse(root_url).path.rsplit("/", 1)[-1]

        def row_file(li):
            a = self._row_anchor(li)
            href = (a.get("href") or "") if a else ""
            return href.rsplit("/", 1)[-1].split("?")[0]

        section = next((li for li in rows if root_file and row_file(li) == root_file), None)
        if section is None:
            section = next((li for li in rows if "nav-open" in (li.get("class") or [])), None)
        if section is None:
            # Fall back to all top-level rows (rows not nested under another nav-row).
            out: list[TocEntry] = []
            for li in rows:
                if not li.find_parent("li", class_="nav-row"):
                    self._walk(li, 0, None, root_url, out)
            return out

        out: list[TocEntry] = []
        self._walk(section, 0, None, root_url, out)
        return out


PROFILE = CommvaultProfile()
registry.register(PROFILE)
