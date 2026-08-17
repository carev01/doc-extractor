"""Salesforce Help documentation profile.

Salesforce Help (help.salesforce.com) is a Lightning/Experience-Cloud SPA.
The doc-set TOC renders as an SLDS tree. In the newer "xcloud" experience the
markup is:

    <ul class="tree opened-tree">
        <li aria-level="1" title="…">
            <div class="slds-tree__item">
                <button aria-expanded="true|false">…</button>   (parents only)
                <a href="…articleView?id=<KEY>&type=5">Title</a>
            </div>
            <ul class="opened-tree"> … child <li> … </ul>          (when expanded)
        </li>
        …
    </ul>

Two things changed from the older tree and broke the previous extractor:
1. ``<li>`` no longer carries ``role="treeitem"`` (it was the sole fingerprint),
   only ``aria-level``.
2. The tree is **lazy**: a collapsed parent's child ``<ul>`` is not in the DOM
   until its toggle is clicked. A single render therefore exposes only the branch
   already expanded to the active page.

So TOC discovery now depth-first *expands* the tree via Browserless
(``expand_salesforce_tree``) rather than reading a one-shot render. The source
URL is the **subtree anchor**: we capture that page and its descendants only,
identified by the ``id=<KEY>`` query param. The same article can appear multiple
times in the tree, so it is deduplicated by key, keeping the first occurrence.

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
        """Build the ordered TOC from the lazy SLDS nav tree via Browserless.

        The source URL is treated as the **subtree anchor**: we capture that page
        plus its descendants, but not its siblings or the parent chain (the
        caller's requirement — "only the TOC below this URL"). The article key is
        the ``id=<KEY>`` query param; when the URL carries no key (a doc-set root),
        the whole ``ul.tree`` is walked.

        ``scraper.expand_salesforce_tree`` depth-first expands the (lazy) tree and
        returns ``{title, href, level}`` with a 0-based depth relative to the
        anchor. We then:
        1. Deduplicate by article id, keeping the first occurrence.
        2. Assign parent_url via a level stack.
        """
        anchor_id = _article_id(root_url)
        items = await scraper.expand_salesforce_tree(root_url, anchor_id)

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
                level = max(0, int(item.get("level", 0)))
            except (ValueError, TypeError):
                level = 0

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
