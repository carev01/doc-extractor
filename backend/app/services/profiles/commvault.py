"""Commvault documentation profile — the original hardcoded logic, now a profile.

TOC: recursively scrape the #nav sidebar, expanding parents (data-is-parent) by
re-scraping each parent URL whose nav exposes the active branch's children.
Content: the #doc element.
"""

import logging

from bs4 import BeautifulSoup

from app.services.profiles import registry
from app.services.profiles.base import TocEntry

logger = logging.getLogger(__name__)


class CommvaultProfile:
    name = "commvault"

    def detect(self, root_html: str, root_url: str) -> bool:
        return 'id="nav"' in root_html and "nav-group" in root_html

    def content_config(self) -> dict:
        return {"includeTags": ["#doc"], "onlyMainContent": False, "waitFor": 1500}

    def _parse_nav_items(self, ul_el) -> list[dict]:
        items = []
        for li in ul_el.find_all("li", class_="nav-row", recursive=False):
            div = li.find(class_="nav-item")
            if not div:
                continue
            a_tag = div.find("a")
            if not a_tag:
                continue
            href = a_tag.get("href", "").strip()
            title = a_tag.get_text(strip=True)
            is_parent = div.has_attr("data-is-parent")
            if href and title:
                items.append({"title": title, "url": href, "is_parent": is_parent})
        return items

    async def build_toc(self, root_url: str, scraper) -> list[TocEntry]:
        return await self._recurse(root_url, 0, set(), {}, None, scraper)

    async def _recurse(self, url, level, visited, cache, parent_url, scraper) -> list[TocEntry]:
        if url in visited:
            return []
        visited.add(url)

        if url not in cache:
            try:
                cache[url] = await scraper.get_html(url)
            except Exception as exc:
                logger.warning("TOC scrape failed for %s: %s", url, exc)
                return []
        html = cache[url]
        if not html:
            return []

        soup = BeautifulSoup(html, "html.parser")
        nav = soup.find(id="nav")
        if not nav:
            return []

        if level == 0:
            root_ul = nav.find("ul", class_="nav-group-root") or nav.find("ul", class_="nav-group")
            if not root_ul:
                return []
            items = self._parse_nav_items(root_ul)
        else:
            active_div = nav.find(class_="nav-item-active")
            if not active_div:
                return []
            children_ul = active_div.parent.find("ul", class_="nav-group")
            if not children_ul:
                return []
            items = self._parse_nav_items(children_ul)

        toc: list[TocEntry] = []
        for item in items:
            toc.append(TocEntry(
                title=item["title"], url=item["url"], level=level,
                is_article=True, parent_url=parent_url,
            ))
            if item["is_parent"]:
                toc.extend(await self._recurse(
                    item["url"], level + 1, visited, cache, item["url"], scraper
                ))
        return toc


PROFILE = CommvaultProfile()
registry.register(PROFILE)
