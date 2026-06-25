"""Warm-up + list-group support-manuals profile.

Some vendors host product guides at ``<host>/support/manuals/<lang>/<family>/<guide>``.
Two things make these hard:

* **A WAF (Akamai) blocks our scraper egress.** A cold Firecrawl scrape (and a
  plain Browserless render) gets an Akamai "Access Denied" page, not the
  content. We defeat it with a **warm-up navigation** (visit the site root first
  so Akamai's JS sets its clearance cookies in the session, then load the
  target) — see :meth:`Scraper.warmup_render`. Because Firecrawl can't do that
  warm-up, both TOC discovery *and* content scraping go through Browserless
  (``render_engine = "browserless"``).
* **Geo localises the page.** Our egress geolocates outside the US, so the page
  chrome/path redirect to a local language even for an ``/en-us/`` URL. The
  article *body*, however, honours the ``lang=en-us`` query param, and the
  ``guid=guid-…`` param is the stable topic key (the slug is cosmetic). So every
  URL we touch is normalised to ``/en-us/`` + ``lang=en-us``.

TOC structure: a Bootstrap list-group under ``<ul id="toc-main-parent-ul">``.
Each ``<li class="list-group-item">`` has a direct-child ``<a class="list-group-link">``
(its own title + ``?guid=`` href) and, for sections, a sibling expand button plus a
nested ``<ul class="list-group collapse" id="childOfguid-…">`` of children. The
children are **already in the DOM** (only CSS-collapsed), so one render yields the
whole tree — no clicking. Hierarchy = ``<li>`` nesting; every node is also a page.

Content body is ``#divTopicContent``.

Detection: cold root HTML is the WAF block page, so an HTML fingerprint is
useless; we match by the publisher host + ``/support/manuals/`` path instead.
"""

import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

from app.services.profiles import registry
from app.services.profiles.base import TocEntry

_TOC_ROOT_SELECTOR = "#toc-main-parent-ul"
_CONTENT_SELECTOR = "#divTopicContent"
# The publisher host whose support-manuals site this profile targets, and the
# path prefix its guides live under. Detection is host/path based because the
# cold HTML is a WAF block page.
_PUBLISHER_HOST = "dell.com"
_MANUALS_PATH = "/support/manuals/"
_WARMUP_URL = "https://www.dell.com/support/home/en-us"
_MANUALS_LANG_RE = re.compile(r"(/support/manuals/)[^/]+/")


def _to_en_us(href: str, base_url: str) -> str:
    """Resolve *href* against *base_url* and force English: ``/manuals/en-us/``
    path segment and a ``lang=en-us`` query param. The ``guid`` param (the stable
    topic key) is preserved; the localised slug is left as-is since the topic
    resolves by guid regardless."""
    u = urljoin(base_url, href)
    p = urlparse(u)
    path = _MANUALS_LANG_RE.sub(r"\1en-us/", p.path)
    q = dict(parse_qsl(p.query, keep_blank_values=True))
    q["lang"] = "en-us"
    return urlunparse((p.scheme or "https", p.netloc, path, p.params, urlencode(q), ""))


def parse_listgroup_toc(html: str, root_url: str) -> list[TocEntry]:
    """Parse the ``#toc-main-parent-ul`` list-group into an ordered TOC.

    Walks the ``<li>`` nesting: each item's own ``<a class="list-group-link">``
    (the first direct-child anchor) is the entry; a nested ``<ul>`` holds its
    children. URLs are normalised to ``/en-us/`` + ``lang=en-us``.
    """
    soup = BeautifulSoup(html, "html.parser")
    root = soup.select_one(_TOC_ROOT_SELECTOR)
    if root is None:
        # warmup_render returns the container's outerHTML, so the root may BE the
        # parsed document's top node; fall back to the first matching <ul>.
        root = soup.find("ul")
    out: list[TocEntry] = []

    def direct_anchor(li):
        for ch in li.children:
            if getattr(ch, "name", None) == "a":
                return ch
        return li.find("a", class_="list-group-link", recursive=False)

    def walk(ul, level: int, parent_url: str | None) -> None:
        for li in ul.find_all("li", recursive=False):
            child_ul = li.find("ul", recursive=False)
            a = direct_anchor(li)
            href = a.get("href") if a is not None else None
            if not href:
                # Url-less node: skip it but keep its children under the same parent.
                if child_ul is not None:
                    walk(child_ul, level, parent_url)
                continue
            title = a.get_text(strip=True)
            url = _to_en_us(href, root_url)
            out.append(TocEntry(
                title=title, url=url, level=level,
                is_article=True, parent_url=parent_url,
            ))
            if child_ul is not None:
                walk(child_ul, level + 1, url)

    if root is not None:
        walk(root, 0, None)
    return out


class WarmupListGroupProfile:
    name = "warmup_listgroup"
    # The WAF (Akamai) blocks Firecrawl; both TOC and content go through
    # Browserless with a warm-up navigation (see module docstring).
    render_engine = "browserless"

    def detect(self, root_html: str, root_url: str) -> bool:
        """Match by URL — cold root HTML is the WAF block page, so an HTML
        fingerprint can't be relied on."""
        p = urlparse(root_url)
        return p.netloc.endswith(_PUBLISHER_HOST) and _MANUALS_PATH in p.path

    async def build_toc(self, root_url: str, scraper) -> list[TocEntry]:
        toc_url = _to_en_us(root_url, root_url)
        data = await scraper.warmup_render(
            toc_url, selector=_TOC_ROOT_SELECTOR, warmup_url=_WARMUP_URL
        )
        html = (data or {}).get("outerHtml") or ""
        return parse_listgroup_toc(html, root_url)

    def browserless_content_spec(self) -> dict:
        """Tell the Browserless content path how to fetch an article body:
        warm up past the WAF, then take the innerHTML of ``#divTopicContent``."""
        return {"selector": _CONTENT_SELECTOR, "warmup_url": _WARMUP_URL}

    def content_config(self) -> dict:
        # Unused for content (render_engine=browserless), but kept for parity.
        return {
            "includeTags": [_CONTENT_SELECTOR],
            "onlyMainContent": False,
            "waitFor": 4000,
        }


PROFILE = WarmupListGroupProfile()
registry.register(PROFILE)
