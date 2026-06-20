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
    # Salesforce Help renders its nav tree AND article body inside shadow DOM
    # (Lightning Web Components), which Firecrawl can't serialise. Both TOC
    # discovery and content scraping therefore go through Browserless, which can
    # run JS in the page to pierce shadow DOM.
    render_engine = "browserless"

    def detect(self, root_html: str, root_url: str) -> bool:
        """Return True when the page is Salesforce Help.

        Requires both ``"slds-"`` (SLDS Lightning Design System) and
        ``"articleView"`` (Salesforce Help URL pattern) in the raw HTML.
        """
        return "slds-" in root_html and "articleView" in root_html

    async def build_toc(self, root_url: str, scraper) -> list[TocEntry]:
        """Build the ordered TOC from the shadow-DOM nav tree via Browserless.

        ``scraper.render`` returns the SLDS tree items (extracted through shadow
        DOM) as ``{title, href, level}`` in document order. We:
        1. Deduplicate by article id (``id=<KEY>`` query param), keeping the
           first occurrence (the active article repeats at the top).
        2. Convert aria-level (1-based) to a 0-based depth.
        3. Assign parent_url via a level stack.
        """
        data = await scraper.render(root_url)
        items = (data or {}).get("toc") or []

        entries: list[TocEntry] = []
        seen_ids: set[str] = set()          # article key strings
        level_stack: dict[int, str] = {}    # level -> last url at that level

        for item in items:
            href = item.get("href") or ""
            art_id = _article_id(href)
            if not art_id or art_id in seen_ids:
                continue

            title = (item.get("title") or "").strip()
            if not title:
                continue
            seen_ids.add(art_id)

            url = urljoin(root_url, href)

            try:
                raw_level = int(item.get("level", 1))
            except (ValueError, TypeError):
                raw_level = 1
            level = max(0, raw_level - 1)

            parent_url: str | None = level_stack.get(level - 1) if level > 0 else None
            level_stack[level] = url
            for k in [k for k in level_stack if k > level]:
                del level_stack[k]

            entries.append(TocEntry(
                title=title, url=url, level=level, is_article=True, parent_url=parent_url,
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
