"""Salesforce Help documentation profile.

Salesforce Help (help.salesforce.com) is a Lightning/Experience-Cloud SPA.
The doc-set TOC renders as an SLDS tree:

    <ul class="tree opened-tree">
        <li role="treeitem" aria-level="1"> … </li>
        <li role="treeitem" aria-level="2"> … </li>
        …
    </ul>

Each ``<li>`` has an ``aria-level`` attribute (values 1–8) encoding depth.
All items link to ``…articleView?id=<KEY>.htm&type=5``.

The same article can appear multiple times in the rendered tree (e.g. the
currently active article appears at the top).  Deduplication is done by the
``id=<KEY>`` query-parameter value, preserving the first occurrence (document
order).

Content body is in ``.slds-text-longform``; the page title is in ``<h1>``.

Detection fingerprint
---------------------
``"slds-"`` (thousands of occurrences in every SLDS/Lightning page) combined
with ``"articleView"`` (the canonical Salesforce Help article URL fragment) is
a narrow, reliable fingerprint.  Neither marker appears in any of the other
supported platform fixtures.
"""

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from app.services.profiles import registry
from app.services.profiles.base import TocEntry

# Matches the id= query-parameter in a Salesforce Help article URL.
# e.g. id=platform.own_from_salesforce.htm  (no &)
_ARTICLE_ID_RE = re.compile(r"[?&]id=([^&]+)")


def _article_id(href: str) -> str | None:
    """Return the article key from an articleView href, or None."""
    m = _ARTICLE_ID_RE.search(href)
    return m.group(1) if m else None


class SalesforceProfile:
    name = "salesforce"

    def detect(self, root_html: str, root_url: str) -> bool:
        """Return True when the page is Salesforce Help.

        Requires both ``"slds-"`` (SLDS Lightning Design System) and
        ``"articleView"`` (Salesforce Help URL pattern) in the raw HTML.
        """
        return "slds-" in root_html and "articleView" in root_html

    async def build_toc(self, root_url: str, scraper) -> list[TocEntry]:
        """Parse the SLDS aria-level tree into an ordered TOC.

        Steps:
        1. Scrape with a 9-second waitFor (the Lightning SPA needs time).
        2. Select all ``li[role=treeitem]`` inside ``ul.tree`` in document order.
        3. For each: extract the ``<a>`` that links to an articleView URL,
           compute level = aria-level - 1 (so the root node is level 0),
           assign parent_url via a level stack.
        4. Deduplicate by article id (``id=<KEY>`` query param), keeping the
           first occurrence.
        """
        html = await scraper.get_html(root_url, wait_ms=9000)
        if not html:
            return []

        soup = BeautifulSoup(html, "html.parser")

        # Scope to the SLDS tree container if present; fall back to the whole doc.
        tree_ul = soup.select_one("ul.tree")
        container = tree_ul if tree_ul else soup

        items = container.select("li[role=treeitem]")

        entries: list[TocEntry] = []
        seen_ids: set[str] = set()          # article key strings
        level_stack: dict[int, str] = {}    # level -> last url at that level

        for li in items:
            # Find the <a> whose href contains "articleView"
            a = li.find("a", href=lambda h: h and "articleView" in h)
            if not a:
                continue

            href = a.get("href", "")
            art_id = _article_id(href)
            if not art_id:
                continue

            # Deduplicate by article id — keep first occurrence.
            if art_id in seen_ids:
                continue
            seen_ids.add(art_id)

            title = a.get_text(strip=True)
            if not title:
                continue

            url = urljoin(root_url, href)

            # Compute 0-based level from aria-level attribute.
            try:
                raw_level = int(li.get("aria-level", 1))
            except (ValueError, TypeError):
                raw_level = 1
            level = max(0, raw_level - 1)

            # Parent is the last URL recorded one level up.
            parent_url: str | None = level_stack.get(level - 1) if level > 0 else None

            # Update level stack; invalidate deeper levels.
            level_stack[level] = url
            for k in list(level_stack.keys()):
                if k > level:
                    del level_stack[k]

            entries.append(TocEntry(
                title=title,
                url=url,
                level=level,
                is_article=True,
                parent_url=parent_url,
            ))

        return entries

    def content_config(self) -> dict:
        """Salesforce Help content extraction config.

        ``.slds-text-longform`` wraps the rendered article body.
        ``onlyMainContent=False`` is required because the page uses a
        non-standard Lightning layout.
        9-second ``waitFor`` for the Lightning SPA to hydrate.
        """
        return {
            "includeTags": [".slds-text-longform"],
            "onlyMainContent": False,
            "waitFor": 9000,
        }


PROFILE = SalesforceProfile()
registry.register(PROFILE)
