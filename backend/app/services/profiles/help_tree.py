"""Rendered help-tree documentation profile (Synology Knowledge Center et al.).

These portals are JS SPAs: a plain GET returns an empty ``#root`` shell, and the
left nav (``#js-sidebar``) is a single global tree spanning *every* product,
built client-side as nested ``<div class="help-tree-node tree_layer_N">`` blocks
(``.row .flex a`` is the link; children sit under ``.nodes .inner``). Two things
broke naive extraction:

  1. The SPA needs several seconds to build the nav, far longer than the default
     1.5s sidebar wait — so a quick render saw an empty tree (1 entry).
  2. The tree isn't ``<ul>/<li>`` and covers all products at once.

This profile renders with a long wait and parses the ``help-tree-node`` blocks,
**scoping to the one product bundle in the source URL** (``/help/<Bundle>/``), so
a source tracks exactly its product's guide — including the sub-feature
hierarchy — out of the shared global tree. The product bundle the source belongs
to is the currently-open (fully-rendered) section, so one render exposes its
whole subtree. Content (``div.help-page``) is also client-rendered, so it goes
through the normal Firecrawl render path (not raw_http).
"""

import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from app.services.profiles import registry
from app.services.profiles.base import TocEntry

# /help/<Bundle>/ — one bundle == one product guide within the global tree.
_BUNDLE_RE = re.compile(r"/help/[^/]+/")
_LAYER_RE = re.compile(r"tree_layer_(\d+)")
# The SPA builds the nav client-side; it needs well over the default 1.5s.
_RENDER_WAIT_MS = 9000


def _layer(node) -> int | None:
    for cls in node.get("class") or []:
        m = _LAYER_RE.fullmatch(cls)
        if m:
            return int(m.group(1))
    return None


def parse_help_tree(html: str, root_url: str, *, nav_selector: str = "#js-sidebar") -> list[TocEntry]:
    """Parse the rendered help-tree, keeping only the source URL's bundle.

    Levels come from each node's ``tree_layer_N`` class (normalised so the
    shallowest kept node is level 0); parentage from the nearest kept ancestor
    ``help-tree-node``. The ``?version=`` query is preserved so content fetches
    hit the same product version the source was added for.
    """
    soup = BeautifulSoup(html, "html.parser")
    nav = soup.select_one(nav_selector)
    if not nav:
        return []
    m = _BUNDLE_RE.search(urlparse(root_url).path)
    prefix = m.group(0) if m else None

    kept: list[tuple[int, str, str, object]] = []  # (layer, title, url, node)
    for node in nav.select(".help-tree-node"):
        row = node.find("div", class_="row")
        a = row.find("a", href=True) if row else None
        if not a:
            continue
        full = urljoin(root_url, a["href"]).split("#", 1)[0]
        if prefix and prefix not in urlparse(full).path:
            continue
        # Strip screen-reader toggle labels Flare-style themes inject in the link.
        for junk in a.select(".invisible-label, .sr-only"):
            junk.decompose()
        title = a.get_text(strip=True)
        kept.append((_layer(node) or 0, title, full, node))

    if not kept:
        return []
    base = min(k[0] for k in kept)
    index = {id(node): i for i, (_, _, _, node) in enumerate(kept)}

    out: list[TocEntry] = []
    for layer, title, url, node in kept:
        parent_url = None
        p = node.parent
        while p is not None:
            if id(p) in index:
                parent_url = kept[index[id(p)]][2]
                break
            p = p.parent
        out.append(TocEntry(
            title=title or url, url=url, level=layer - base,
            is_article=True, parent_url=parent_url,
        ))
    return out


class HelpTreeProfile:
    name = "help_tree"

    def detect(self, root_html: str, root_url: str) -> bool:
        return (
            "help-tree-node" in root_html
            or "js-sidebar" in root_html
            or "SYNO_WEB" in root_html
        )

    async def build_toc(self, root_url: str, scraper) -> list[TocEntry]:
        html = await scraper.get_html(root_url, _RENDER_WAIT_MS)
        return parse_help_tree(html, root_url)

    def content_config(self) -> dict:
        # Content is client-rendered; render (not raw_http) with a long wait so
        # the SPA has mounted the article body. Scope to #kb_help_body (the KB
        # article container) rather than div.help-page — the latter also wraps
        # the entire #js-sidebar nav (hundreds of links rendered before the
        # article) and the subheader tab bar. Drop the in-body feedback widget
        # (".feedBackForm" — a sibling of the prose) and the empty section
        # selector so neither leaks into the markdown.
        return {
            "includeTags": ["#kb_help_body"],
            "excludeTags": [".feedBackForm", ".section-selector-container"],
            "onlyMainContent": False,
            "waitFor": _RENDER_WAIT_MS,
        }


PROFILE = HelpTreeProfile()
registry.register(PROFILE)
