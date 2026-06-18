"""Reusable TOC-acquisition strategies shared by platform profiles.

- sidebar_tree_toc: parse a nested <ul>/<li><a> nav into an ordered TOC.
- hubspoke_toc: crawl root -> categories -> (sections) -> articles (help centers).
- sitemap_urls: enumerate URLs from sitemap.xml in document order.
"""

from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .base import TocEntry


async def sidebar_tree_toc(
    scraper, root_url: str, nav_selector: str, *, item_selector: str = "a", wait_ms: int = 1500
) -> list[TocEntry]:
    """Parse the nested list under ``nav_selector`` into an ordered TOC.

    Pass the nav container element OR the list (<ul>) itself — if the selected
    element is already a ``<ul>``, it is used directly as the top-level list.

    A node with a child <ul> is treated as a section (is_article=False); a leaf
    link is an article. Order is the DOM order of the nav.
    """
    soup = BeautifulSoup(await scraper.get_html(root_url, wait_ms), "html.parser")
    nav = soup.select_one(nav_selector)
    out: list[TocEntry] = []
    if not nav:
        return out

    def walk(ul, level: int, parent_url: str | None) -> None:
        for li in ul.find_all("li", recursive=False):
            a = li.find(item_selector)
            # Prefer a direct child <ul>; fall back to any descendant <ul> to
            # handle wrappers like MkDocs Material's <li><nav><ul>…</ul></nav></li>.
            child_ul = li.find("ul", recursive=False) or li.find("ul")
            if not a or not a.get("href"):
                # Section label without its own link: descend, keeping the parent.
                if child_ul:
                    walk(child_ul, level, parent_url)
                continue
            url = urljoin(root_url, a["href"])
            out.append(TocEntry(
                title=a.get_text(strip=True), url=url, level=level,
                is_article=child_ul is None, parent_url=parent_url,
            ))
            if child_ul:
                walk(child_ul, level + 1, url)

    top = nav if nav.name == "ul" else nav.find("ul")
    if top:
        walk(top, 0, None)
    return out


async def hubspoke_toc(
    scraper, root_url: str, *, category_link_selector: str, article_link_selector: str,
    section_link_selector: str | None = None,
) -> list[TocEntry]:
    """Crawl a help-center hub: root -> categories -> (optional sections) -> articles."""
    root = BeautifulSoup(await scraper.get_html(root_url), "html.parser")
    out: list[TocEntry] = []
    seen: set[str] = set()
    for cat in root.select(category_link_selector):
        if not cat.get("href"):
            continue
        cat_url = urljoin(root_url, cat["href"])
        if cat_url in seen:
            continue
        seen.add(cat_url)
        out.append(TocEntry(cat.get_text(strip=True), cat_url, 0, False, None))
        cat_soup = BeautifulSoup(await scraper.get_html(cat_url), "html.parser")

        if section_link_selector:
            sections = [(s.get_text(strip=True), urljoin(cat_url, s["href"]))
                        for s in cat_soup.select(section_link_selector) if s.get("href")]
        else:
            sections = [(None, cat_url)]

        for sec_title, sec_url in sections:
            if sec_title is None:
                sec_soup, parent, alevel = cat_soup, cat_url, 1
            else:
                if sec_url in seen:
                    continue
                seen.add(sec_url)
                out.append(TocEntry(sec_title, sec_url, 1, False, cat_url))
                sec_soup = BeautifulSoup(await scraper.get_html(sec_url), "html.parser")
                parent, alevel = sec_url, 2
            for art in sec_soup.select(article_link_selector):
                if not art.get("href"):
                    continue
                art_url = urljoin(sec_url, art["href"])
                if art_url in seen:
                    continue
                seen.add(art_url)
                out.append(TocEntry(art.get_text(strip=True), art_url, alevel, True, parent))
    return out


async def sitemap_urls(scraper, root_url: str) -> list[str]:
    """Return all <loc> URLs from the site's sitemap.xml, in document order."""
    parsed = urlparse(root_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    xml = await scraper.get_html(urljoin(base + "/", "sitemap.xml"))
    soup = BeautifulSoup(xml, "html.parser")
    return [loc.get_text(strip=True) for loc in soup.find_all("loc")]
