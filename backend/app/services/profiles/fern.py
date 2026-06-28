"""Fern docs profile (full nav tree + bodies server-rendered into static HTML).

Targets documentation built on the Fern framework (buildwithfern.com), e.g.
docs.eon.io. The complete sidebar (aside.fern-sidebar-desktop, a nested
<ul>/<li> tree) and the article body (.fern-prose) are server-rendered into
every page, so the profile runs on the raw_http path. For login-walled Fern
sites the realm session is injected as cookies by the authenticated raw_http
path (see _select_content_path / fetch_raw); the profile itself is unchanged
whether or not auth is in play.
"""

from app.services.profiles import registry
from app.services.profiles.base import TocEntry
from app.services.profiles.strategies import parse_sidebar_tree

_NAV_SELECTOR = "aside.fern-sidebar-desktop"


class FernProfile:
    name = "fern"
    content_engine = "raw_http"

    def detect(self, root_html: str, root_url: str) -> bool:
        # The sidebar hook plus the article hook together are distinctive to Fern.
        return "fern-sidebar" in root_html and "fern-prose" in root_html

    async def build_toc(self, root_url: str, scraper) -> list[TocEntry]:
        try:
            html = await scraper.get_raw(root_url)
        except Exception:
            return []
        return parse_sidebar_tree(html or "", root_url, _NAV_SELECTOR)

    def content_config(self) -> dict:
        return {
            "includeTags": [".fern-prose"],
            # Drop the right-rail "on this page" TOC, breadcrumb/nav, and footer.
            "excludeTags": [".toc-root", "nav", "footer"],
            "onlyMainContent": False,
        }


PROFILE = FernProfile()
registry.register(PROFILE)
