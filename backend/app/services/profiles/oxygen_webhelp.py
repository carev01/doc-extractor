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


# ── Post-scrape TOC rebuild ──────────────────────────────────────────────────
# Oxygen tocids embed a generated build id: "tocId-d33886e917" belongs to build
# 33886. Within one build a tocid is a reliable *position* id — a topic reused
# under two parents gets two tocids, and a heading that borrows its child's page
# gets its own. Across builds it means nothing: the same topic carries a
# different tocid in every build. A portal serves pages from several builds at
# once (only republished pages pick up the new one), so the rebuild stitches
# each build on its own and then overlays them newest-first, matching the same
# topic across builds by (url, k) — see rebuild_toc.

_BUILD_RE = re.compile(r"-d(\d+)e\d+$")


class _Node:
    __slots__ = ("url", "k", "title", "children")

    def __init__(self, url, k, title):
        self.url, self.k, self.title, self.children = url, k, title, []

    @property
    def key(self):
        return (self.url, self.k)


def _own_link(li, page_url):
    """(url, title) of *li*'s own link — never a descendant item's.
    ``li.find("a")`` falls through to a child's anchor for an item without a link
    of its own, mis-titling it and splicing the child into its place."""
    if li is None:
        return None
    for a in li.find_all("a", href=True):
        if a.find_parent("li", attrs={"role": "treeitem"}) is li:
            return (urldefrag(urljoin(page_url, a["href"]))[0],
                    a.get_text(strip=True) or a["href"])
    return None


def _same_url_depth(li, url, page_url):
    """k: how many consecutive ancestor items link to the same *url*. The
    outermost heading of such a run is k=0, the real topic under it k=1."""
    k, up = 0, li.find_parent("li", attrs={"role": "treeitem"})
    while up is not None:
        above = _own_link(up, page_url)
        if not above or above[0] != url:
            break
        k += 1
        up = up.find_parent("li", attrs={"role": "treeitem"})
    return k


def _direct_child_items(container):
    out = []
    for ul in container.find_all("ul", recursive=False):
        out.extend([li for li in ul.find_all("li", recursive=False)
                    if li.get("role") == "treeitem"])
    return out


def _merge_ordered(into: list, new: list) -> None:
    """Ordered union: keep *into*'s order and put each unseen item of *new* just
    before the next item of *new* that *into* already has, else at the end —
    honouring both orders without shuffling what is already placed."""
    for i, item in enumerate(new):
        if item in into:
            continue
        succ = next((new[j] for j in range(i + 1, len(new)) if new[j] in into), None)
        into.insert(into.index(succ) if succ is not None else len(into), item)


def _build_of(frag: str):
    """The build a fragment was published in: the id its tocids share. Every
    fragment of Rubrik's RSC portal has exactly one (4,885 of 4,885)."""
    ids = re.findall(r'data-tocid="[^"]*?-d(\d+)e\d+"', frag or "")
    return max(set(ids), key=ids.count) if ids else None


def _stitch_build(fragments) -> list:
    """One build's tree, keyed by tocid (a reliable position id within a build).
    Items without a tocid fall back to their (url, k)."""
    info: dict = {}                 # pid -> (url, k, title)
    parent_of: dict = {}
    children: dict = {}
    top: list = []

    def pid(li, page_url):
        link = _own_link(li, page_url)
        if not link:
            return None
        tid = li.get("data-tocid") or ("url", link[0], _same_url_depth(li, link[0], page_url))
        if tid not in info:
            info[tid] = (link[0], _same_url_depth(li, link[0], page_url), link[1])
        return tid

    def pids(items, page_url):
        out = []
        for li in items:
            t = pid(li, page_url)
            if t is not None and t not in out:
                out.append(t)
        return out

    for page_url, frag in sorted(fragments, key=lambda f: f[0]):
        soup = BeautifulSoup(frag or "", "html.parser")
        nav = (soup.select("#wh_publication_toc") or [soup])[0]
        _merge_ordered(top, pids(_direct_child_items(nav), page_url))
        for li in nav.find_all("li", attrs={"role": "treeitem"}):
            t = pid(li, page_url)
            if t is None:
                continue
            pli = li.find_parent("li", attrs={"role": "treeitem"})
            p = pid(pli, page_url) if pli is not None else None
            if p is not None and p != t and t not in parent_of:
                parent_of[t] = p
            kids = [c for c in pids(_direct_child_items(li), page_url) if c != t]
            if kids:
                _merge_ordered(children.setdefault(t, []), kids)

    listed = {c for kids in children.values() for c in kids}
    for t, p in parent_of.items():
        if t not in listed and p in info:
            children.setdefault(p, []).append(t)
    roots = [t for t in top if t not in parent_of] or [t for t in info if t not in parent_of]

    seen: set = set()

    def build(t):
        if t in seen or t not in info:
            return None
        seen.add(t)
        url, k, title = info[t]
        node = _Node(url, k, title)
        node.children = [n for n in (build(c) for c in children.get(t, [])) if n]
        return node

    tree = [n for n in (build(t) for t in roots) if n]
    for t in sorted(info, key=str):          # anything unreached → top level
        if t not in seen:
            n = build(t)
            if n:
                tree.append(n)
    return tree


def _overlay(base: list, extra: list) -> None:
    """Graft an older build's tree onto *base* (newer), in place.

    A topic *base* already has — matched by (url, k) — keeps its newer position:
    that is how a page that moved between builds loses its stale spot. Only
    topics *base* lacks are added, under their (mapped) parent, so an old-build
    page nobody republished still lands where its own build put it.
    """
    index: dict = {}

    def walk(nodes):
        for n in nodes:
            index.setdefault(n.key, n)
            walk(n.children)
    walk(base)

    def graft(nodes, target_list):
        for n in nodes:
            if n.key in index:
                target = index[n.key]
            else:
                target = _Node(n.url, n.k, n.title)
                index[n.key] = target
                # Before the next sibling (in *extra*'s order) that base has.
                succ = next((index[m.key] for m in nodes[nodes.index(n) + 1:]
                             if m.key in index and index[m.key] in target_list), None)
                target_list.insert(target_list.index(succ) if succ else len(target_list), target)
            graft(n.children, target.children)

    graft(extra, base)


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

        **Per build, then overlaid newest-first.** Each fragment carries the id
        of the publishing build its page came from (the ``d<n>`` in every tocid;
        every RSC fragment has exactly one). Within a build a tocid is a sound
        position id, so a topic genuinely reused under two parents keeps one
        entry per context, each with the sub-pages of *that* context (RSC's
        "Legal hold for snapshot retention" under both IBM Db2 and SAP HANA).
        Across builds tocids mean nothing, and mixing them tripled RSC's tree —
        9,268 entries for 4,891 pages, one copy per build.

        Builds are overlaid newest-first, matching topics by ``(url, k)`` (``k``
        = consecutive ancestors sharing the URL, which keeps a heading that
        borrows its child's page distinct from that child). A topic the newer
        tree already has keeps its newer position, which is how a page that
        moved between builds sheds its stale one; only topics the newer tree
        lacks are grafted in, where their own build put them. Newer = higher
        build id: DITA-OT's ``d<n>`` counts documents processed, and on RSC it
        matched publish order exactly — all 2,096 pages Rubrik changed on
        2026-09-24 and all 551 new ones carry the highest id.

        Why not a single identity for everything: tocid alone tripled the tree;
        URL alone merged Oxygen headings into the child whose page they borrow
        (71 pairs on RSC) and put a reused topic's sub-pages all in one context;
        URL + title breaks on the 124 RSC URLs retitled between builds.

        A heading (a node with a same-URL child) is emitted without a URL; its
        children nest by level. A reused topic yields one entry per context with
        the same URL, which the persistence layer handles (parents resolve by
        parent_url in DFS order; the article links to one of them). Children are
        an ordered union across a build's fragments, strays attach under their
        known parent, and fragments are processed in URL order — the caller
        reads them without ORDER BY and every first-seen choice depends on it.
        """
        by_build: dict = {}
        for page_url, frag in fragments:
            by_build.setdefault(_build_of(frag), []).append((page_url, frag))
        if not by_build:
            return []
        order = sorted(by_build, key=lambda b: (b is None, -int(b) if b else 0))
        tree = _stitch_build(by_build[order[0]])
        for b in order[1:]:
            _overlay(tree, _stitch_build(by_build[b]))

        out: list[TocEntry] = []

        def emit(nodes, level, parent_url):
            for n in nodes:
                heading = any(c.url == n.url for c in n.children)
                url = None if heading else n.url
                out.append(TocEntry(title=n.title, url=url, level=level,
                                    is_article=not heading, parent_url=parent_url))
                emit(n.children, level + 1, url)

        emit(tree, 0, None)
        return out

    def content_config(self) -> dict:
        return {
            "includeTags": ["article"],
            "excludeTags": [".related-links", "nav", "header", "footer", ".wh_breadcrumb"],
            "onlyMainContent": False,
        }


PROFILE = OxygenWebhelpProfile()
registry.register(PROFILE)
