"""Fern docs profile (full nav + bodies server-rendered into static HTML).

Targets documentation built on the Fern framework (buildwithfern.com), e.g.
docs.eon.io. The article body (.fern-prose) and the full set of sidebar links
are server-rendered into every page, so the profile runs on the raw_http path.
For login-walled Fern sites the realm session is injected as cookies by the
authenticated raw_http path (see _select_content_path / fetch_raw); the profile
is unchanged whether or not auth is in play.

In the *raw* (no-JS) HTML the sidebar (``aside.fern-sidebar-desktop``) is NOT a
single nested <ul>/<li> tree: the page links sit in a flat <ul> alongside a tab
switcher and collapsible-section <button>s. So we collect every in-guide anchor
in DOM order and derive nesting from URL path depth (the same approach as the
rspress profile), which is robust to the raw DOM shape.
"""

import posixpath
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from app.services.profiles import registry
from app.services.profiles.base import TocEntry

_SIDEBAR = "aside.fern-sidebar-desktop"


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
        soup = BeautifulSoup(html or "", "html.parser")
        side = soup.select_one(_SIDEBAR)
        if not side:
            return []

        # The guide's top path segment (e.g. "/user-guide/") — excludes the other
        # docs tab (e.g. "/api/...") and any off-guide chrome links.
        root_segs = [s for s in urlparse(root_url).path.split("/") if s]
        top = "/" + root_segs[0] + "/" if root_segs else "/"

        # Collect in-guide anchors in DOM order, de-duped.
        seen: set[str] = set()
        nodes: list[tuple[str, str]] = []
        for a in side.find_all("a", href=True):
            url = urljoin(root_url, a["href"])
            if not urlparse(url).path.startswith(top):
                continue
            if url in seen:
                continue
            seen.add(url)
            nodes.append((a.get_text(strip=True) or url, url))
        if not nodes:
            return []

        # Level = URL path depth below the guide root (longest common directory
        # prefix of the links): 1 segment → 0, 2 → 1, 3 → 2.
        guide_root = posixpath.commonpath([posixpath.dirname(urlparse(u).path) for _, u in nodes])

        def level(url: str) -> int:
            rel = urlparse(url).path[len(guide_root):].lstrip("/")
            return len(rel.split("/")) - 1

        return [
            TocEntry(title=title, url=url, level=level(url), is_article=True)
            for title, url in nodes
        ]

    def content_config(self) -> dict:
        return {
            "includeTags": [".fern-prose"],
            # Drop the right-rail "on this page" TOC, breadcrumb/nav, and footer.
            "excludeTags": [".toc-root", "nav", "footer"],
            "onlyMainContent": False,
        }


PROFILE = FernProfile()
registry.register(PROFILE)
