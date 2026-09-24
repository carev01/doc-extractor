"""Oxygen XML WebHelp profile (e.g. Rubrik docs.rubrik.com).

Login-walled + WAF-protected. Content is server-rendered in-page (``article``);
the full TOC is not in any single page (the on-page #wh_publication_toc is
contextual) and there is no TOC file — but Oxygen's search index ships a
complete page inventory at
``<pub_root>/oxygen-webhelp/app/search/index/htmlFileInfoList.js``. build_toc
uses that for a complete (flat, URL-ordered) TOC; the authored hierarchy is
rebuilt post-scrape from per-page #wh_publication_toc fragments (see rebuild_toc,
added later). Runs on the authenticated raw-HTTP path, paced for the WAF.
"""

import json
import re
from urllib.parse import urljoin, urldefrag

from bs4 import BeautifulSoup

from app.services.profiles import registry
from app.services.profiles.base import TocEntry

_INVENTORY_REL = "oxygen-webhelp/app/search/index/htmlFileInfoList.js"
_OXYGEN_REF = re.compile(r'["\'](?:[^"\']*?/)?oxygen-webhelp/')


def _pub_root(page_url: str, html: str) -> str | None:
    """The publication root: the absolute URL up to (and including) the segment
    before ``oxygen-webhelp/``, derived from an oxygen-webhelp asset ref."""
    m = _OXYGEN_REF.search(html)
    if not m:
        return None
    ref = m.group(0).strip('"\'')
    abs_ref = urljoin(page_url, ref)            # .../en-us/saas/oxygen-webhelp/
    return abs_ref.split("oxygen-webhelp/")[0]  # .../en-us/saas/


class OxygenWebhelpProfile:
    name = "oxygen_webhelp"
    content_engine = "raw_http"
    # WAF pacing (read by the raw-HTTP content path).
    raw_http_concurrency = 2
    raw_http_request_delay = 0.3
    raw_http_retry_statuses = (429, 502, 503, 504)
    # Capture each page's TOC tree fragment for the post-process hierarchy rebuild.
    toc_fragment_selector = "#wh_publication_toc"

    def detect(self, root_html: str, root_url: str) -> bool:
        return "oxygen-webhelp" in root_html and "wh_publication_toc" in root_html

    async def build_toc(self, root_url: str, scraper) -> list[TocEntry]:
        try:
            html = await scraper.get_raw(root_url)
        except Exception:
            return []
        pub_root = _pub_root(root_url, html or "")
        if not pub_root:
            return []
        try:
            raw = await scraper.get_raw(pub_root + _INVENTORY_REL)
        except Exception:
            return []
        m = re.search(r"htmlFileInfoList\s*=\s*(\[.*\])", raw or "", re.S)
        if not m:
            return []
        try:
            entries = json.loads(m.group(1))
        except Exception:
            return []
        out: list[TocEntry] = []
        seen: set[str] = set()
        for s in entries:
            parts = s.split("@@@")
            path = parts[0].strip()
            if not path or path in seen:
                continue
            seen.add(path)
            title = parts[1].strip() if len(parts) > 1 and parts[1].strip() else path
            out.append(TocEntry(
                title=title, url=urljoin(pub_root, path),
                level=path.count("/"), is_article=True,
            ))
        return out

    def rebuild_toc(self, fragments: "list[tuple[str, str]]", root_url: str) -> list[TocEntry]:
        """Stitch per-page TOC fragments into one authored hierarchy.

        **Node identity is the topic's URL plus its depth within a run of
        same-URL ancestors** — ``(url, k)`` — and neither of the two obvious
        keys works:

        * ``data-tocid`` looks stable and is not. It embeds a generated
          publishing-build id (``tocId-d30118e917``), and a portal whose pages
          come from different builds hands out different tocids for one topic:
          Rubrik's RSC docs carry ``tocId-d30118e917``, ``tocId-d30697e917`` and
          ``getting_started_with_rsc-d30118e923`` for the same page, and 1,163 of
          2,672 URLs in a 600-page sample had more than one. Keyed by tocid, each
          id scheme became its own copy of the tree — 9,268 entries for 4,891
          pages, every top-level section three times.
        * The URL alone is stable, but Oxygen section headings routinely link to
          their first child's page (Security → Users and Access both point at
          ``users_access.html``; 71 such pairs in RSC). Keyed by URL, each heading
          merged into its child and a level of the manual vanished. Adding the
          title doesn't rescue it either: 124 RSC URLs carry different titles in
          different builds ("Node removal from RSC" / "Node removal in RSC").

        ``k`` counts the consecutive ancestors that share the item's URL, so the
        outermost heading is ``(url, 0)`` and the real topic under it
        ``(url, 1)`` — true in every build, and whether or not a given fragment
        happens to show that heading expanded. For every URL without such a pair
        ``k`` is 0 and the key is just the URL. A heading is then emitted without
        a URL (a section; its children nest by level), and the page belongs to
        the deepest item, which is the actual topic — so every URL still maps to
        exactly one entry and one article.

        A topic genuinely listed under two different parents appears once, at
        its first position in the walk, matching the one-article-one-TOC-
        position model the rest of the pipeline assumes.

        Children are an ordered **union** across fragments, not the longest list
        any one fragment showed: pages from different builds list slightly
        different children, and "longest wins" orphaned the rest to the top
        level (184 roots for a 16-section manual). A node no child list reaches
        but whose parent is known is attached under that parent for the same
        reason. Fragments are processed in URL order: the caller reads them with
        no ORDER BY and every "first seen" choice depends on order, so without it
        the same corpus produced a different tree on each run.
        """
        Key = tuple  # (url, k)
        title_of: dict[Key, str] = {}
        parent_of: dict[Key, Key] = {}
        children: dict[Key, list[Key]] = {}
        top_order: list[Key] = []

        def own_url(li, page_url):
            """(url, title) of *li*'s own link — never a descendant item's.
            ``li.find("a")`` would fall through to a child's anchor for an item
            without a link of its own, mis-titling it and splicing the child in."""
            for a in li.find_all("a", href=True):
                if a.find_parent("li", attrs={"role": "treeitem"}) is li:
                    return (urldefrag(urljoin(page_url, a["href"]))[0],
                            a.get_text(strip=True) or a["href"])
            return None

        def key_of(li, page_url):
            """(url, k) for *li*, or None when it has no link of its own."""
            link = own_url(li, page_url)
            if not link:
                return None
            url, k, up = link[0], 0, li.find_parent("li", attrs={"role": "treeitem"})
            while up is not None:
                above = own_url(up, page_url)
                if not above or above[0] != url:
                    break
                k += 1
                up = up.find_parent("li", attrs={"role": "treeitem"})
            return (url, k)

        def direct_child_items(container):
            # treeitem <li> that are this container's nearest treeitem descendants
            out = []
            for ul in container.find_all("ul", recursive=False):
                out.extend([li for li in ul.find_all("li", recursive=False)
                            if li.get("role") == "treeitem"])
            return out

        def keys_of(items, page_url):
            seen_here, out = set(), []
            for li in items:
                k = key_of(li, page_url)
                if k and k not in seen_here:
                    seen_here.add(k)
                    out.append(k)
            return out

        def merge(into: list, new: list) -> None:
            """Ordered union: keep *into*'s order, and put each unseen item from
            *new* just before the next item of *new* that *into* already has —
            or at the end when none follows. Inserting before the successor
            (rather than after the predecessor) honours both lists' ordering
            without shuffling siblings that are already placed."""
            for i, item in enumerate(new):
                if item in into:
                    continue
                succ = next((new[j] for j in range(i + 1, len(new)) if new[j] in into), None)
                into.insert(into.index(succ) if succ is not None else len(into), item)

        for page_url, frag in sorted(fragments, key=lambda f: f[0]):
            soup = BeautifulSoup(frag or "", "html.parser")
            navs = soup.select("#wh_publication_toc") or [soup]
            nav = navs[0]
            merge(top_order, keys_of(direct_child_items(nav), page_url))
            for li in nav.find_all("li", attrs={"role": "treeitem"}):
                key = key_of(li, page_url)
                if key is None:
                    continue
                title_of.setdefault(key, own_url(li, page_url)[1])
                pli = li.find_parent("li", attrs={"role": "treeitem"})
                pkey = key_of(pli, page_url) if pli is not None else None
                if pkey and pkey != key and key not in parent_of:
                    parent_of[key] = pkey
                kids = [c for c in keys_of(direct_child_items(li), page_url) if c != key]
                if kids:
                    merge(children.setdefault(key, []), kids)

        if not title_of:
            return []
        # A node no child list mentions, but whose parent is known, belongs under
        # that parent — not appended at the top level after everything else.
        listed = {c for kids in children.values() for c in kids}
        for key, parent in parent_of.items():
            if key not in listed and parent in title_of:
                children.setdefault(parent, []).append(key)
        top_order = [k for k in top_order if k not in parent_of]

        out: list[TocEntry] = []
        seen: set = set()

        def walk(key, level: int, parent_url):
            if key in seen or key not in title_of:
                return
            seen.add(key)
            # A heading that borrows its child's page is a section: the page
            # belongs to the child, which is the actual topic.
            heading = any(c[0] == key[0] for c in children.get(key, []))
            url = None if heading else key[0]
            out.append(TocEntry(title=title_of[key], url=url, level=level,
                                is_article=not heading, parent_url=parent_url))
            for c in children.get(key, []):
                walk(c, level + 1, url)

        roots = top_order or [k for k in title_of if k not in parent_of]
        for k in roots:
            walk(k, 0, None)
        for k in sorted(title_of):   # any unreached nodes → append at top
            if k not in seen:
                walk(k, 0, None)
        return out

    def content_config(self) -> dict:
        return {
            "includeTags": ["article"],
            "excludeTags": [".related-links", "nav", "header", "footer", ".wh_breadcrumb"],
            "onlyMainContent": False,
        }


PROFILE = OxygenWebhelpProfile()
registry.register(PROFILE)
