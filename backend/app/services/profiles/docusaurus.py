"""Docusaurus documentation profile.

TOC: parse the nested sidebar nav via sidebar_tree_toc using the
``nav[aria-label="Docs sidebar"]`` selector, which is a stable Docusaurus
landmark.  The shared strategy walks ``<li>/<a>`` + nested ``<ul>``.
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
            scraper, root_url, 'nav[aria-label="Docs sidebar"]'
        )

    def content_config(self) -> dict:
        return {
            "includeTags": [".theme-doc-markdown"],
            "onlyMainContent": False,
            "waitFor": 1500,
        }


PROFILE = DocusaurusProfile()
registry.register(PROFILE)
