"""Confluence Cloud documentation profile.

TOC STRATEGY
============
Confluence Cloud renders its page-tree navigation via a React virtualised
component that Firecrawl cannot reliably render — the tree collapses, hides
most nodes, or simply never appears.  So the full hierarchy is built from
Confluence's REST API instead:

    GET <wiki>/rest/api/content?spaceKey=<KEY>&type=page
        &expand=ancestors,extensions.position

A single (paginated) call returns every page in the space with its
``ancestors`` (the last of which is the direct parent) and
``extensions.position`` (the sibling sort key).  Sorting each sibling group by
position ascending reproduces the curated page-tree order exactly, and the
ancestor chain gives the nesting depth — so the resulting ``TocEntry`` list is
the full, ordered hierarchy.

FALLBACK (REST unavailable)
---------------------------
Some spaces disable anonymous REST access.  When the API call fails (auth
error, no results, malformed JSON), we fall back to the previous BEST-EFFORT
behaviour: collect the page links rendered inside the overview's
``.wiki-content`` body as flat level-0 entries.  This is not a full hierarchy
(deeply nested pages are absent) but it keeps such spaces working.

Detection fingerprint
---------------------
Both ``"confluence"`` and ``"atlassian"`` appear in every Confluence Cloud
page (body id, CSS class names, server-performance span, Atlaskit portal).
These markers are absent from all other supported platform fixtures.
"""

import json
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from app.services.profiles import registry
from app.services.profiles.base import TocEntry

# Matches any href that contains both /wiki/spaces/ and /pages/ — the
# canonical pattern for Confluence page links.
_PAGE_LINK_RE = re.compile(r"/wiki/spaces/[^/]+/pages/")

# Captures the space KEY from a Confluence URL path (.../spaces/<KEY>/...).
_SPACE_KEY_RE = re.compile(r"/spaces/([^/]+)")


def _normalise_page_url(href: str, root_url: str) -> str:
    """Return an absolute URL.  The href may already be absolute or relative."""
    return urljoin(root_url, href)


def _page_id_from_url(url: str) -> str | None:
    """Extract the numeric page ID from a Confluence page URL.

    Both ``/wiki/spaces/KEY/pages/12345/Title`` and
    ``/wiki/spaces/KEY/pages/12345`` forms are handled.
    """
    m = re.search(r"/pages/(\d+)", url)
    return m.group(1) if m else None


class ConfluenceProfile:
    name = "confluence"

    # REST pagination: page size per request, and a hard cap on total pages
    # collected (safety bound for very large spaces).
    REST_PAGE_LIMIT = 100
    REST_MAX_PAGES = 5000

    def detect(self, root_html: str, root_url: str) -> bool:
        """Return True only when the page is structurally Confluence.

        Earlier this keyed on the words ``"confluence"`` and ``"atlassian"``
        appearing together, but that false-positives on any page that merely
        *talks about* Atlassian Confluence — e.g. a vendor changelog for a
        Confluence backup connector. Match a structural fingerprint instead:
        ``com-atlassian-confluence`` (the Confluence page-wrapper element id) or
        ``/wiki/spaces/`` (Confluence's space/page URL scheme). Both are present
        on real Confluence instances and absent from pages that only reference
        the product.
        """
        lower = root_html.lower()
        return "com-atlassian-confluence" in lower or "/wiki/spaces/" in lower

    async def build_toc(self, root_url: str, scraper) -> list[TocEntry]:
        """Build the full, ordered page-tree from the REST API.

        Prefer the REST hierarchy (every page, correctly nested and ordered);
        fall back to scraping the rendered overview links when the API isn't
        anonymously available — see the module docstring.
        """
        entries = await self._build_toc_via_rest(root_url, scraper)
        if entries:
            return entries
        return await self._build_toc_from_rendered_links(root_url, scraper)

    # ── REST hierarchy ──────────────────────────────────────────────────────

    async def _build_toc_via_rest(self, root_url: str, scraper) -> list[TocEntry]:
        space_key = self._space_key(root_url)
        wiki_base = self._wiki_base(root_url)
        if not space_key or not wiki_base:
            return []
        pages = await self._fetch_all_pages(scraper, wiki_base, space_key)
        if not pages:
            return []
        return self._build_tree(pages, wiki_base)

    @staticmethod
    def _space_key(url: str) -> str | None:
        m = _SPACE_KEY_RE.search(urlparse(url).path)
        return m.group(1) if m else None

    @staticmethod
    def _wiki_base(url: str) -> str | None:
        """Return the scheme://host plus the ``/wiki`` context path, no trailing slash.

        REST endpoints and page ``webui`` links are both rooted at this base
        (e.g. ``https://host/wiki/rest/api/...`` and ``/spaces/KEY/...``).
        """
        p = urlparse(url)
        idx = p.path.find("/wiki")
        if idx == -1:
            return None
        return f"{p.scheme}://{p.netloc}{p.path[:idx + len('/wiki')]}"

    async def _fetch_all_pages(self, scraper, wiki_base: str, space_key: str) -> dict:
        """Fetch every page in the space via the REST content API (paginated).

        Returns {page_id: {id, title, parent, position, webui}}.  Returns an
        empty dict on any failure so the caller falls back to link-scraping.
        """
        pages: dict[str, dict] = {}
        start = 0
        while len(pages) < self.REST_MAX_PAGES:
            url = (
                f"{wiki_base}/rest/api/content?spaceKey={space_key}&type=page"
                f"&expand=ancestors,extensions.position"
                f"&limit={self.REST_PAGE_LIMIT}&start={start}"
            )
            try:
                raw = await scraper.get_raw(url)
                data = json.loads(raw)
            except Exception:
                return {}  # REST unavailable / not JSON → signal fallback

            results = data.get("results") or []
            if not results:
                break
            for p in results:
                pid = str(p.get("id"))
                ancestors = p.get("ancestors") or []
                parent = str(ancestors[-1]["id"]) if ancestors else None
                position = (p.get("extensions") or {}).get("position")
                if not isinstance(position, int):
                    position = 0
                title = (p.get("title") or "").strip()
                webui = ((p.get("_links") or {}).get("webui")) or ""
                if pid and title and webui:
                    pages[pid] = {
                        "id": pid,
                        "title": title,
                        "parent": parent,
                        "position": position,
                        "webui": webui,
                    }
            if not (data.get("_links") or {}).get("next"):
                break
            start += self.REST_PAGE_LIMIT
        return pages

    def _build_tree(self, pages: dict, wiki_base: str) -> list[TocEntry]:
        """Resolve the flat {id: page} map into an ordered, hierarchical TOC."""
        # Group children by parent id; sort each group by sibling position.
        children: dict[str | None, list[dict]] = {}
        for p in pages.values():
            children.setdefault(p["parent"], []).append(p)
        for siblings in children.values():
            siblings.sort(key=lambda p: (p["position"], p["title"]))

        # Roots = pages with no parent, plus any whose parent is outside the
        # space (defensive: an ancestor we didn't collect).
        root_keys = [
            k for k in children
            if k is None or k not in pages
        ]

        out: list[TocEntry] = []
        visited: set[str] = set()

        def walk(parent_key: str | None, level: int, parent_url: str | None) -> None:
            for p in children.get(parent_key, []):
                if p["id"] in visited:  # guard against pathological cycles
                    continue
                visited.add(p["id"])
                url = wiki_base + p["webui"] if p["webui"].startswith("/") else p["webui"]
                out.append(TocEntry(
                    title=p["title"],
                    url=url,
                    level=level,
                    is_article=True,
                    parent_url=parent_url,
                ))
                walk(p["id"], level + 1, url)

        for key in root_keys:
            walk(key, 0, None)
        return out

    # ── Fallback: rendered overview links ────────────────────────────────────

    async def _build_toc_from_rendered_links(self, root_url: str, scraper) -> list[TocEntry]:
        """BEST-EFFORT fallback: collect rendered Confluence page links as
        flat level-0 entries (used only when the REST API is unavailable).

        Scrapes the root overview page with a long ``waitFor`` so the React app
        hydrates, then collects ``<a href>`` links containing both
        ``/wiki/spaces/`` and ``/pages/``, de-duplicated by numeric page ID
        (keeping the canonical title-slug form).
        """
        html = await scraper.get_html(root_url, wait_ms=9000)
        if not html:
            return []

        soup = BeautifulSoup(html, "html.parser")
        entries: list[TocEntry] = []
        seen_page_ids: dict[str, int] = {}  # page_id -> index in entries

        for a in soup.find_all("a", href=_PAGE_LINK_RE):
            href = a.get("href", "")
            if not href:
                continue

            url = _normalise_page_url(href, root_url)
            page_id = _page_id_from_url(url)
            if not page_id:
                continue

            title = a.get_text(strip=True)
            if not title:
                continue

            if page_id in seen_page_ids:
                # Prefer the URL form that contains a title slug (longer path).
                existing_idx = seen_page_ids[page_id]
                existing = entries[existing_idx]
                if len(url) > len(existing.url):
                    entries[existing_idx] = TocEntry(
                        title=title, url=url, level=0, is_article=True, parent_url=None,
                    )
            else:
                seen_page_ids[page_id] = len(entries)
                entries.append(TocEntry(
                    title=title, url=url, level=0, is_article=True, parent_url=None,
                ))

        return entries

    def content_config(self) -> dict:
        """Confluence Cloud content extraction config.

        ``.wiki-content`` is the stable CSS class wrapping the rendered page
        body in both Confluence Cloud and Confluence Data Center.
        ``onlyMainContent=False`` is required because Confluence wraps the
        page in several non-standard containers that Firecrawl's main-content
        heuristic strips by default.
        A 9-second ``waitFor`` is necessary for the React SPA to hydrate.
        """
        return {
            "includeTags": [".wiki-content"],
            "onlyMainContent": False,
            "waitFor": 9000,
        }


PROFILE = ConfluenceProfile()
registry.register(PROFILE)
