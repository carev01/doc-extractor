"""Commvault documentation profile.

The current documentation.commvault.com platform renders its sidebar nav
client-side into ``#nav`` (it reads "Loading…" in static/Firecrawl HTML), so the
TOC is built from the **Browserless-rendered** nav: we wait for ``.nav-row`` to
appear, then walk the nested ``<ul>``/``<li class="nav-row">`` tree, scoped to
the active section (the bookshelf rooted at the requested URL).

Article content lives in server-rendered ``#doc``, so content scraping uses the
normal Firecrawl path (no per-article browser render needed).
"""

import logging
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from app.services.profiles import registry
from app.services.profiles.base import TocEntry

logger = logging.getLogger(__name__)


class CommvaultProfile:
    name = "commvault"

    def detect(self, root_html: str, root_url: str) -> bool:
        # New platform: documentation.commvault.com (nav is "Loading…" client-side,
        # so key off the host / cv- markers). Old platform: inline #nav + nav-group.
        host = urlparse(root_url).netloc
        if host.endswith("documentation.commvault.com") or "cv-nav-slug" in root_html:
            return True
        return 'id="nav"' in root_html and "nav-group" in root_html

    def content_config(self) -> dict:
        # #doc holds the article, but starts with a ">" breadcrumb trail; drop it.
        return {
            "includeTags": ["#doc"],
            "excludeTags": [".breadcrumbs"],
            "onlyMainContent": False,
            "waitFor": 1500,
        }

    @staticmethod
    def _row_anchor(li):
        """The row's own <a> (inside its .nav-item label, not a nested child)."""
        item = li.find(class_="nav-item")
        a = item.find("a") if item else li.find("a")
        return a

    @staticmethod
    def _child_rows(li):
        """Direct child nav-rows (the row's own nested <ul>)."""
        child_ul = li.find("ul")
        if not child_ul:
            return []
        return child_ul.find_all("li", class_="nav-row", recursive=False)

    def _walk(self, li, level, parent_url, root_url, out):
        a = self._row_anchor(li)
        href = (a.get("href") or "").strip() if a else ""
        title = a.get_text(strip=True) if a else ""
        if not href or not title:
            return
        url = urljoin(root_url, href)
        children = self._child_rows(li)
        out.append(TocEntry(
            title=title, url=url, level=level,
            is_article=not children, parent_url=parent_url,
        ))
        for child in children:
            self._walk(child, level + 1, url, root_url, out)

    async def build_toc(self, root_url: str, scraper) -> list[TocEntry]:
        """Render the sidebar via Browserless and return the active section's tree.

        Scopes to the bookshelf the user rooted at: the nav-row whose link is the
        requested page (the active/open section), plus its nested descendants.
        """
        html = await scraper.get_rendered_html(root_url, wait_for=".nav-row")
        if not html:
            return []
        soup = BeautifulSoup(html, "html.parser")
        nav = soup.find(id="nav")
        if not nav:
            return []

        rows = nav.find_all("li", class_="nav-row")
        if not rows:
            return []

        # Find the section to scope to: the row whose link matches the requested
        # page (by filename), else the open/active row, else the first top-level row.
        root_file = urlparse(root_url).path.rsplit("/", 1)[-1]

        def row_file(li):
            a = self._row_anchor(li)
            href = (a.get("href") or "") if a else ""
            return href.rsplit("/", 1)[-1].split("?")[0]

        section = next((li for li in rows if root_file and row_file(li) == root_file), None)
        if section is None:
            section = next((li for li in rows if "nav-open" in (li.get("class") or [])), None)
        if section is None:
            # Fall back to all top-level rows (rows not nested under another nav-row).
            out: list[TocEntry] = []
            for li in rows:
                if not li.find_parent("li", class_="nav-row"):
                    self._walk(li, 0, None, root_url, out)
            return out

        out: list[TocEntry] = []
        self._walk(section, 0, None, root_url, out)
        return out


PROFILE = CommvaultProfile()
registry.register(PROFILE)
