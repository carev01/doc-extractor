"""Collapsible-sidebar documentation profile.

For doc portals built as a Next.js app whose left-nav is a shadcn/ui sidebar
made of nested **radix Collapsible** components. Each guide/section is a
``<div data-slot="collapsible">`` wrapping a label ``<li data-slot="sidebar-menu-item">``
and a sibling ``<div data-slot="collapsible-content">``; the content's child
``<ul data-slot="sidebar-menu">`` is NOT mounted in the DOM until the trigger is
clicked. A one-shot render therefore exposes only the top-level guides (observed:
74 guides, 10 links) and the generic ``<ul>/<li>/<a>`` sidebar walker finds
nothing — the ``<div data-slot="collapsible">`` wrapper sits between each ``<ul>``
and its ``<li>``, breaking the strict direct-child nesting that walker expects.

We therefore:

* **build_toc** — expand the whole sidebar in Browserless (clicking every
  collapsed ``collapsible-trigger`` until the tree is fully mounted), then parse
  the result with :func:`parse_collapsible_sidebar`, which understands the
  collapsible wrapper. Falls back to a single render (top level only) if
  Browserless is unavailable.
* **content_config** — the article body is the single ``<article class="prose">``
  element (the sidebar lives inside ``<main>``, so ``includeTags=["main"]`` would
  drag the whole nav into every page).

Sections with a trigger but no link of their own become url-less TocEntry nodes
that their children nest under; a section that is also a page carries its ``<a>``.
"""

from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from app.services.profiles import registry
from app.services.profiles.base import TocEntry

_SIDEBAR_SELECTOR = "div[data-slot='sidebar-inner']"


def parse_collapsible_sidebar(html: str, root_url: str) -> list[TocEntry]:
    """Parse an (expanded) shadcn/radix collapsible sidebar into an ordered TOC.

    Hierarchy comes from the nesting of ``ul[data-slot='sidebar-menu']``. Each
    menu entry is either a plain ``li[data-slot='sidebar-menu-item']`` (leaf) or
    a ``div[data-slot='collapsible']`` wrapping that ``li`` plus a sibling
    ``div[data-slot='collapsible-content'] > ul[data-slot='sidebar-menu']``
    (children). An entry's label is its own ``<a href>`` (article, possibly also a
    parent) or its ``collapsible-trigger`` button text (url-less section). Because
    the children live in the *sibling* content div — not inside the label ``<li>``
    — scoping the label lookup to the ``<li>`` never picks up a child's link.
    """
    soup = BeautifulSoup(html, "html.parser")
    nav = soup.select_one(_SIDEBAR_SELECTOR) or soup
    out: list[TocEntry] = []

    def menu_entries(ul):
        """Yield (label_li, child_ul_or_None) for each entry directly under *ul*."""
        for child in ul.find_all(["li", "div"], recursive=False):
            slot = child.get("data-slot")
            if slot == "collapsible":
                li = child.find("li", attrs={"data-slot": "sidebar-menu-item"}, recursive=False)
                content = child.find("div", attrs={"data-slot": "collapsible-content"}, recursive=False)
                child_ul = content.find("ul", attrs={"data-slot": "sidebar-menu"}) if content else None
                if li is not None:
                    yield li, child_ul
            elif slot == "sidebar-menu-item" and child.name == "li":
                yield child, None

    def label(li):
        a = li.find("a", href=True)
        btn = li.find("button", attrs={"data-slot": "collapsible-trigger"})
        el = a or btn
        title = el.get_text(strip=True) if el else ""
        url = urljoin(root_url, a["href"]) if a else None
        return title, url

    def walk(ul, level: int, parent_url: str | None) -> None:
        for li, child_ul in menu_entries(ul):
            title, url = label(li)
            if not title:
                continue
            out.append(TocEntry(
                title=title, url=url, level=level,
                is_article=bool(url) and child_ul is None,
                parent_url=parent_url,
            ))
            if child_ul is not None:
                walk(child_ul, level + 1, url or parent_url)

    # Top-level menus: every sidebar-menu <ul> with no sidebar-menu ancestor.
    for ul in nav.select("ul[data-slot='sidebar-menu']"):
        if ul.find_parent("ul", attrs={"data-slot": "sidebar-menu"}) is None:
            walk(ul, 0, None)
    return out


class CollapsibleSidebarProfile:
    name = "collapsible_sidebar"

    def detect(self, root_html: str, root_url: str) -> bool:
        host = urlparse(root_url).netloc
        if host.endswith("docs.cohesity.com"):
            return True
        # Same platform on another host: shadcn sidebar + radix collapsible nav.
        return (
            "data-slot=\"sidebar-inner\"" in root_html
            and "data-slot=\"collapsible-trigger\"" in root_html
        )

    async def build_toc(self, root_url: str, scraper) -> list[TocEntry]:
        from app.services.browserless import BrowserlessError
        import logging
        logger = logging.getLogger(__name__)
        try:
            html = await scraper.expand_collapsible_sidebar(root_url)
            entries = parse_collapsible_sidebar(html, root_url)
            if entries:
                return entries
            logger.warning(
                "Collapsible-sidebar expand for %s yielded no entries — falling "
                "back to single render", root_url,
            )
        except BrowserlessError as exc:
            logger.warning(
                "Collapsible-sidebar expand failed for %s (%s) — falling back to "
                "single render (top level only)", root_url, exc,
            )
        # Fallback: parse whatever the single render exposes (top level only).
        html = await scraper.get_html(root_url)
        return parse_collapsible_sidebar(html, root_url)

    def content_config(self) -> dict:
        # Article body is <article class="prose">; the sidebar nav lives inside
        # <main>, so target the article directly to avoid dragging in the TOC.
        return {
            "includeTags": ["article"],
            "onlyMainContent": False,
            "waitFor": 2000,
        }


PROFILE = CollapsibleSidebarProfile()
registry.register(PROFILE)
