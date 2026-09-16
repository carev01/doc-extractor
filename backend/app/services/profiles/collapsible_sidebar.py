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

import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from app.services.profiles import registry
from app.services.profiles.base import TocEntry

_SIDEBAR_SELECTOR = "div[data-slot='sidebar-inner']"

# ── zDocs URL grammar ────────────────────────────────────────────────────────
# This platform addresses a page as
#   /docs/<product>/<version>/<publication>-<build>-<variant>/<topic>-<build>
# where <publication> and <topic> are stable identities but <build> is re-minted
# per release AND repeated in both segments. Templating only <version> therefore
# produces a URL that 404s and a topic key that changes wholesale every release —
# see app/services/versioning.py. Measured on NetBackup 11.2 -> 11.2.0.1:
# publication 103228346 and topic v95650213 held, build 171368441 -> 173151032.
_ZDOCS_URL_RE = re.compile(
    r"^(?P<origin>https?://[^/]+)/docs/(?P<product>[^/]+)/(?P<version>[^/]+)"
    r"/(?P<pub>\d+)-(?P<rev>\d+)-(?P<variant>\d+)(?P<tail>/.*)?$"
)
# The same grammar over a template, to recover the parts needed to rebuild a
# landing URL. {rev} has replaced the build id by this point.
_ZDOCS_TMPL_RE = re.compile(
    r"^(?P<origin>https?://[^/]+)/docs/(?P<product>[^/]+)/\{version\}"
    r"/(?P<pub>\d+)-\{rev\}-(?P<variant>\d+)(?:/.*)?$"
)


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
    # Content comes from a direct GET, not a render. The article body is fully
    # server-rendered — a leaf topic's <article> carries 5,180 bytes of HTML
    # (26 <p>, 19 <li>) in the raw response — so rendering buys nothing and costs
    # everything: Firecrawl renders through Browserless, whose Chromium this
    # portal 403s, and Firecrawl's per-request `headers` cannot fix that because
    # Playwright ignores a User-Agent set as an extra HTTP header (it must be set
    # on the browser context). Every page therefore came back empty and was
    # skipped. A plain GET returns 200 for the same URLs, and is 20-50x faster.
    #
    # Only the TOC still needs a browser, because the sidebar tree mounts on
    # click; that path sets the UA properly via BrowserlessClient.
    content_engine = "raw_http"

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

    # ── Volatile URL token ({rev}) ──────────────────────────────────────────

    def templatize_url(self, url: str, version: str) -> str | None:
        """Mark both the version and the per-release build id in *url*.

        Returns None for anything outside the zDocs grammar — including a URL
        whose version segment isn't *version* — so a caller falls back to the
        generic version-only detection rather than templating a guess.
        """
        m = _ZDOCS_URL_RE.match(url or "")
        if not m or m.group("version") != version:
            return None
        rev = m.group("rev")
        tail = m.group("tail") or ""
        return (
            f"{m.group('origin')}/docs/{m.group('product')}/{{version}}"
            f"/{m.group('pub')}-{{rev}}-{m.group('variant')}"
            # Replace every occurrence: the build id recurs in the topic segment.
            f"{tail.replace(rev, '{rev}')}"
        )

    async def resolve_revision(self, template: str, version: str, scraper) -> str | None:
        """Read the build id this publication currently carries at *version*.

        The version landing page (``/docs/<product>/<version>``) is static HTML
        served without auth and links every publication at its current build, so
        one plain GET answers this — no render, no Browserless. Scoped to the
        template's own publication id because the build id is minted **per
        publication**, not per version: NetBackup 11.2.0.1 serves 103228346 at
        173151032 and 169433500 at 172152080 on the same page.
        """
        m = _ZDOCS_TMPL_RE.match(template or "")
        if not m or not version:
            return None
        landing = f"{m.group('origin')}/docs/{m.group('product')}/{version}"
        try:
            html = await scraper.get_raw(landing)
        except Exception:
            return None
        hit = re.search(
            rf"/docs/{re.escape(m.group('product'))}/{re.escape(version)}"
            rf"/{m.group('pub')}-(\d+)-{m.group('variant')}\b",
            html or "",
        )
        return hit.group(1) if hit else None

    def extract_content_html(self, raw: str, url: str) -> str | None:
        """Scope a raw page to its ``<article>`` body and absolutise its images.

        Returns None when there is no ``<article>`` at all, which the raw_http
        failure-rate guard reports as a rate (grep "No content body found at" for
        the offending URLs). Section pages legitimately carry only a list of child
        topics — a couple of hundred bytes — so thinness is not emptiness here and
        must not be treated as a miss.
        """
        soup = BeautifulSoup(raw, "html.parser")
        article = soup.select_one("article")
        if article is None:
            return None
        for img in article.find_all("img"):
            src = img.get("src")
            if src:
                img["src"] = urljoin(url, src)
        return str(article)

    def content_config(self) -> dict:
        # Unused on the raw_http path (extract_content_html does the scoping), but
        # extract_source always calls content_config(), so it must exist — and it
        # keeps the selector correct if this profile is ever routed through a
        # render again.
        return {
            "includeTags": ["article"],
            "onlyMainContent": False,
            "waitFor": 2000,
        }


PROFILE = CollapsibleSidebarProfile()
registry.register(PROFILE)
