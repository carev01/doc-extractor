"""Confluence Cloud documentation profile — BEST-EFFORT scraping.

BEST-EFFORT NOTICE
==================
Confluence Cloud renders its page-tree navigation via a React virtualised
component that Firecrawl cannot reliably render — the tree collapses, hides
most nodes, or simply never appears.  What *does* render is the space
overview's inline `.wiki-content` body, which typically includes links to a
handful of top-level child pages.

This profile collects those rendered in-content page links as level-0
``TocEntry`` objects.  It is NOT a full hierarchy: deeply nested or
collapsed pages are silently absent.

FOLLOW-ON: A full hierarchy requires Confluence's REST API
(``/wiki/api/v2/spaces/<KEY>/pages`` + recursive child expansion).
That is intentionally out of scope for this task.

Detection fingerprint
---------------------
Both ``"confluence"`` and ``"atlassian"`` appear in every Confluence Cloud
page (body id, CSS class names, server-performance span, Atlaskit portal).
These markers are absent from all other supported platform fixtures.
"""

import re
from urllib.parse import urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

from app.services.profiles import registry
from app.services.profiles.base import TocEntry

ROOT = "https://documentation.campus.barracuda.com/wiki/spaces/BCCB/overview?homepageId=3244034"

# Matches any href that contains both /wiki/spaces/ and /pages/ — the
# canonical pattern for Confluence page links.
_PAGE_LINK_RE = re.compile(r"/wiki/spaces/[^/]+/pages/")


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

    def detect(self, root_html: str, root_url: str) -> bool:
        """Return True when the page contains Confluence/Atlassian markers.

        Both ``"confluence"`` (e.g. ``id="com-atlassian-confluence"``) and
        ``"atlassian"`` (e.g. Atlaskit portal class, performance span) must
        be present.  Either alone could appear on third-party pages that embed
        Atlassian widgets, but their co-presence is a reliable Confluence
        fingerprint.
        """
        lower = root_html.lower()
        return "confluence" in lower and "atlassian" in lower

    async def build_toc(self, root_url: str, scraper) -> list[TocEntry]:
        """BEST-EFFORT: collect rendered Confluence page links as level-0 entries.

        Scrapes the root overview page with a long ``waitFor`` (9 s) so the
        React app has time to hydrate and render the ``.wiki-content`` body.
        Collects all ``<a href>`` links whose href contains both
        ``/wiki/spaces/`` and ``/pages/``, de-duplicates by numeric page ID
        (keeping the canonical form that includes the title slug), and returns
        them as ordered level-0 ``TocEntry`` objects.

        NOTE: The Confluence Cloud virtualised page-tree sidebar does NOT
        render via Firecrawl.  Only links embedded in the overview's content
        body appear here.  Full hierarchy requires the Confluence REST API —
        see module docstring.
        """
        html = await scraper.get_html(root_url, wait_ms=9000)
        if not html:
            return []

        soup = BeautifulSoup(html, "html.parser")

        # Collect all anchor tags whose href looks like a Confluence page link.
        entries: list[TocEntry] = []
        seen_page_ids: dict[str, int] = {}  # page_id -> index in entries

        for a in soup.find_all("a", href=_PAGE_LINK_RE):
            href = a.get("href", "")
            if not href:
                continue

            url = _normalise_page_url(href, root_url)
            page_id = _page_id_from_url(url)

            # Skip links with no recognisable page ID.
            if not page_id:
                continue

            title = a.get_text(strip=True)
            if not title:
                continue

            if page_id in seen_page_ids:
                # Already recorded this page. Prefer the URL form that contains
                # a title slug (longer path) over the bare-ID form.
                existing_idx = seen_page_ids[page_id]
                existing = entries[existing_idx]
                if len(url) > len(existing.url):
                    entries[existing_idx] = TocEntry(
                        title=title,
                        url=url,
                        level=0,
                        is_article=True,
                        parent_url=None,
                    )
            else:
                seen_page_ids[page_id] = len(entries)
                entries.append(TocEntry(
                    title=title,
                    url=url,
                    level=0,
                    is_article=True,
                    parent_url=None,
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
