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

That URL-depth inference is now only the **fallback**. Current Fern builds embed
the whole sidebar as data in the Next.js flight payload
(``self.__next_f.push``): a tree of ``section`` / ``page`` / ``link`` /
``sidebarGroup`` nodes with titles, slugs and ``hidden`` flags — the exact
hierarchy the site renders. The raw DOM, by contrast, expands only the path to
the current page, and the 160-odd links it does carry sit in a visually hidden,
completely flat accessibility ``<nav>``. Inferring nesting from those produced a
wrong tree on docs.eon.io once Eon restructured: section headings (Cloud
Workloads, GCP, MongoDB Atlas, Resources, ...) are link-less so they vanished,
and every page attached to whichever shallower page preceded it — MongoDB Atlas'
pages ended up nested under GCP's "Regions".
"""

import json
import posixpath
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from app.services.profiles import registry
from app.services.profiles.base import TocEntry

_SIDEBAR = "aside.fern-sidebar-desktop"

# One Next.js flight chunk: self.__next_f.push([<n>, "<JSON-escaped string>"]).
_FLIGHT_PUSH = re.compile(r'self\.__next_f\.push\(\[\d+,\s*("(?:[^"\\]|\\.)*")\]\)')
# The sidebar's root object is the only one that *opens* with its children list
# of typed nav nodes; a section's own "children" never starts its object.
_NAV_ROOT = re.compile(r'\{"children":\[\{"type":"(?:sidebarGroup|section|page|link)"')
_UNDEFINED = "$undefined"


def _nav_tree(html: str):
    """The sidebar tree embedded in the page's flight payload, or None."""
    chunks = []
    for lit in _FLIGHT_PUSH.findall(html or ""):
        try:
            chunks.append(json.loads(lit))
        except ValueError:
            return None
    flight = "".join(chunks)
    m = _NAV_ROOT.search(flight)
    if not m:
        return None
    try:
        root, _ = json.JSONDecoder().raw_decode(flight[m.start():])
    except ValueError:
        return None
    kids = root.get("children") if isinstance(root, dict) else None
    return kids if isinstance(kids, list) and kids else None


def _toc_from_nav(nodes, origin: str) -> list[TocEntry]:
    """Walk Fern nav nodes into TOC entries, depth-first.

    * ``sidebarGroup`` is an untitled container — its children sit at its level.
    * ``section`` is a heading; it carries a URL only when it has an overview
      page of its own (``overviewPageId``), otherwise it is a URL-less section
      whose children nest under it by level.
    * ``page`` is an article at ``origin/<slug>``.
    * ``link`` points off-site and is skipped, as is anything ``hidden`` (the
      site doesn't show it in the sidebar either).
    """
    out: list[TocEntry] = []

    def defined(v):
        return v not in (None, _UNDEFINED, "")

    def walk(items, level, parent_url):
        for n in items or []:
            if not isinstance(n, dict) or n.get("hidden") is True:
                continue
            kind = n.get("type")
            if kind == "sidebarGroup":
                walk(n.get("children"), level, parent_url)
            elif kind == "section":
                url = (f"{origin}/{n['slug']}" if defined(n.get("overviewPageId"))
                       and defined(n.get("slug")) else None)
                out.append(TocEntry(title=n.get("title") or "", url=url, level=level,
                                    is_article=url is not None, parent_url=parent_url))
                walk(n.get("children"), level + 1, url)
            elif kind == "page" and defined(n.get("slug")):
                out.append(TocEntry(title=n.get("title") or n["slug"],
                                    url=f"{origin}/{n['slug']}", level=level,
                                    is_article=True, parent_url=parent_url))

    walk(nodes, 0, None)
    return out


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
        tree = _nav_tree(html)
        if tree:
            parsed = urlparse(root_url)
            entries = _toc_from_nav(tree, f"{parsed.scheme}://{parsed.netloc}")
            if any(e.url for e in entries):
                return entries

        # Fallback for Fern builds that don't embed the nav tree: infer nesting
        # from URL depth over the sidebar's anchors (see module docstring).
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
