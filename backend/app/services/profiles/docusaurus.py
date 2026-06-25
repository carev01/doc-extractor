"""Docusaurus documentation profile.

TOC: Docusaurus does NOT mount a collapsed category's children in the DOM until
it is expanded, so a single render of ``.theme-doc-sidebar-menu`` only exposes
the top-level items (observed on Portworx: 11 of 258 pages). We therefore expand
the sidebar in Browserless first — clicking every collapsed caret until the full
tree is mounted — then parse the resulting HTML with the shared sidebar walker
(``<ul>`` as top-level list, ``<li>/<a>`` + nested ``<ul>``). If Browserless is
unavailable we fall back to a single render (top level only) so a TOC is still
produced. ``.theme-doc-sidebar-menu`` is the stable Docusaurus class
(i18n-robust, unlike the English aria-label).
Content: the ``.theme-doc-markdown`` wrapper.
"""

import logging

from app.services.profiles import registry
from app.services.profiles.strategies import parse_sidebar_tree, sidebar_tree_toc
from app.services.profiles.base import TocEntry

logger = logging.getLogger(__name__)

_NAV_SELECTOR = ".theme-doc-sidebar-menu"


class DocusaurusProfile:
    name = "docusaurus"
    # Hybrid: TOC discovery still renders the sidebar via Browserless
    # (build_toc), but article bodies are static HTML under .theme-doc-markdown
    # (see content_config), so the content phase fetches directly — no render.
    content_engine = "raw_http"

    def detect(self, root_html: str, root_url: str) -> bool:
        return (
            "theme-doc-sidebar-menu" in root_html
            and "theme-doc-sidebar-item-" in root_html
        )

    async def build_toc(self, root_url: str, scraper) -> list[TocEntry]:
        # Import here so the profile stays importable/unit-testable without the
        # browserless module loaded.
        from app.services.browserless import BrowserlessError
        try:
            html = await scraper.expand_docusaurus_sidebar(root_url)
            entries = parse_sidebar_tree(html, root_url, _NAV_SELECTOR)
            if entries:
                return entries
            logger.warning(
                "Docusaurus expand for %s yielded no entries — falling back to "
                "single render", root_url,
            )
        except BrowserlessError as exc:
            logger.warning(
                "Docusaurus sidebar expand failed for %s (%s) — falling back to "
                "single render (top level only)", root_url, exc,
            )
        return await sidebar_tree_toc(scraper, root_url, _NAV_SELECTOR)

    def content_config(self) -> dict:
        return {
            "includeTags": [".theme-doc-markdown"],
            # Docusaurus theme chrome: edit-this-page, prev/next pager,
            # breadcrumbs, tags/last-updated footer. Mostly outside the markdown
            # body, excluded explicitly as defence-in-depth.
            "excludeTags": [
                ".theme-edit-this-page", ".pagination-nav",
                ".theme-doc-footer", ".theme-doc-breadcrumbs", ".theme-last-updated",
            ],
            "onlyMainContent": False,
            "waitFor": 1500,
        }


PROFILE = DocusaurusProfile()
registry.register(PROFILE)
