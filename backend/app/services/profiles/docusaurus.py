"""Docusaurus documentation profile.

TOC: parse the nested sidebar nav via sidebar_tree_toc using the
``.theme-doc-sidebar-menu`` selector, which is the stable Docusaurus class
(i18n-robust, unlike the English aria-label).  The shared strategy treats a
selected ``<ul>`` directly as the top-level list and walks ``<li>/<a>`` +
nested ``<ul>``.
Content: the ``.theme-doc-markdown`` wrapper.
"""

from app.services.profiles import registry
from app.services.profiles.strategies import sidebar_tree_toc
from app.services.profiles.base import TocEntry


class DocusaurusProfile:
    name = "docusaurus"

    def detect(self, root_html: str, root_url: str) -> bool:
        return (
            "theme-doc-sidebar-menu" in root_html
            and "theme-doc-sidebar-item-" in root_html
        )

    async def build_toc(self, root_url: str, scraper) -> list[TocEntry]:
        return await sidebar_tree_toc(
            scraper, root_url, ".theme-doc-sidebar-menu"
        )

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
