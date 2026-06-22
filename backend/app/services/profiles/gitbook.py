"""GitBook documentation profile.

TOC: GitBook renders its sidebar inside:
  <aside data-testid="table-of-contents" data-gb-table-of-contents="true">

The top-level <ul> contains two kinds of <li>:
  - Leaf articles:  <li><a href="...">Title</a></li>  (direct <a> child)
  - Sections:       <li>
                      <div>...</div>       (spacer / scroll sentinel)
                      <div><button>Section Name</button></div>
                      <div>...<ul>         (collapsed/expanded child list)
                        <li><a href="...">Child Title</a></li>
                        ...
                      </ul></div>
                    </li>

Because section <li>s have NO direct <a> child (only a <button> label), the
generic sidebar_tree_toc helper would incorrectly find the first *descendant*
anchor and produce duplicates.  We use a custom walk instead.

CSS classes are Tailwind / hashed and unstable; all selectors use the
stable ``data-testid`` / ``data-gb-table-of-contents`` attributes.

Content: GitBook pages render content inside <main>; ``onlyMainContent=True``
is the cleanest extraction path.  A longer ``waitFor`` (3 s) is used because
GitBook is an SPA that hydrates on the client.
"""

import logging
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from app.services.browserless import BrowserlessError
from app.services.profiles import registry
from app.services.profiles.base import TocEntry

logger = logging.getLogger(__name__)

# Cap pages visited per Browserless session call so a single call stays bounded;
# the crawl spans as many calls as needed (resumable via the checkpoint).
GITBOOK_CRAWL_BATCH = 50


def _path_of(url: str) -> str:
    return urlparse(url).path.rstrip("/")


def _direct_children(aside_html: str, page_url: str, root_url: str) -> list[tuple[str, str]]:
    """Ordered (url, title) of the sidebar node whose link matches ``page_url``.

    GitBook only expands the current page's node, so this returns exactly that
    node's direct children (collapsed grandchildren aren't in the DOM).
    """
    if not aside_html:
        return []
    soup = BeautifulSoup(aside_html, "html.parser")
    want = _path_of(page_url)
    target = None
    for a in soup.find_all("a", href=True):
        # The in-tree link lives inside an <li>; skip header/breadcrumb anchors.
        if _path_of(urljoin(root_url, a["href"])) == want and a.find_parent("li") is not None:
            target = a
            break
    if target is None:
        return []
    li = target.find_parent("li")
    sub = li.find("ul")
    if not sub:
        return []
    kids: list[tuple[str, str]] = []
    for cli in sub.find_all("li", recursive=False):
        a = cli.find("a", href=True)
        if a:
            kids.append((urljoin(root_url, a["href"]), a.get_text(strip=True)))
    return kids


def _walk_gitbook_ul(ul, level: int, parent_url: str | None, root_url: str, out: list[TocEntry]) -> None:
    """Recursively walk a GitBook <ul> into TocEntry objects.

    GitBook top-level <li>s are either:
      - Leaf articles  — have a direct <a> child, no nested <ul>.
      - Sections       — have a <button> label and a descendant <ul> but NO
                         direct <a> child.  We treat the button text as the
                         section title and emit a non-article TocEntry, then
                         descend into the child <ul> at the next level.

    Any <li> with no <a> and no nested <ul> is skipped.
    """
    for li in ul.find_all("li", recursive=False):
        direct_a = li.find("a", recursive=False)
        child_ul = li.find("ul", recursive=False) or li.find("ul")

        if direct_a and direct_a.get("href"):
            # Leaf article or section-with-link
            url = urljoin(root_url, direct_a["href"])
            out.append(TocEntry(
                title=direct_a.get_text(strip=True),
                url=url,
                level=level,
                is_article=child_ul is None,
                parent_url=parent_url,
            ))
            if child_ul:
                _walk_gitbook_ul(child_ul, level + 1, url, root_url, out)
        elif child_ul:
            # Section with button label but no direct anchor.
            # Emit a non-article TocEntry using the button text; URL is empty
            # because GitBook section headers are expand/collapse buttons, not links.
            btn = li.find("button")
            section_title = btn.get_text(strip=True) if btn else ""
            if section_title:
                out.append(TocEntry(
                    title=section_title,
                    url="",
                    level=level,
                    is_article=False,
                    parent_url=parent_url,
                ))
            _walk_gitbook_ul(child_ul, level + 1, parent_url, root_url, out)
        # else: no anchor, no child ul — skip


class GitBookProfile:
    name = "gitbook"

    def detect(self, root_html: str, root_url: str) -> bool:
        return "data-gb-table-of-contents" in root_html

    async def build_toc(self, root_url: str, scraper) -> list[TocEntry]:
        """Reconstruct the full ordered tree.

        GitBook's sidebar is *contextual*: on any page it renders the top-level
        sections, their direct children, and only the **current** page's direct
        children — deeper levels appear only when you navigate into a page. So a
        single render captures just the first sub-level. We crawl every page via
        Browserless, merge each page's revealed direct children, and assemble the
        ordered tree. The crawl is checkpointed (resumes after interruption).

        Falls back to the single-render walk (first sub-level only) if Browserless
        is unavailable.
        """
        checkpoint = getattr(scraper, "checkpoint", None)
        try:
            base, children = await self._crawl(root_url, scraper, checkpoint)
            return self._assemble(base, children, root_url)
        except BrowserlessError as exc:
            logger.warning(
                "GitBook crawl via Browserless failed (%s); falling back to single render",
                exc,
            )
            return await self._single_render_toc(root_url, scraper)

    async def _single_render_toc(self, root_url: str, scraper) -> list[TocEntry]:
        """Original behaviour: one Firecrawl render, walk whatever is in the DOM
        (top-level + first sub-level only). Fallback when Browserless is absent."""
        html = await scraper.get_html(root_url, 3000)
        soup = BeautifulSoup(html, "html.parser")
        aside = soup.select_one('aside[data-testid="table-of-contents"]')
        out: list[TocEntry] = []
        if not aside:
            return out
        top_ul = aside.find("ul")
        if top_ul:
            _walk_gitbook_ul(top_ul, 0, None, root_url, out)
        return out

    @staticmethod
    async def _crawl(root_url: str, scraper, checkpoint):
        """BFS over pages: render the root for the top two levels, then visit every
        discovered page and collect its node's direct children. Returns
        (base_nodes, children_map[path -> [(url, title), ...]])."""
        state = await checkpoint.load() if checkpoint else {}

        base_data = state.get("gb_base")
        if base_data is None:
            root_html = (await scraper.gitbook_sidebars([root_url])).get(root_url, "")
            if not root_html:
                raise BrowserlessError("empty GitBook root sidebar")
            soup = BeautifulSoup(root_html, "html.parser")
            base: list[TocEntry] = []
            top_ul = soup.find("ul")
            if top_ul:
                _walk_gitbook_ul(top_ul, 0, None, root_url, base)
            base_data = [
                {"title": e.title, "url": e.url, "level": e.level, "is_article": e.is_article}
                for e in base
            ]
            if checkpoint:
                await checkpoint.save_data({"gb_base": base_data})
        else:
            base = [TocEntry(d["title"], d["url"], d["level"], d["is_article"]) for d in base_data]

        children: dict[str, list[tuple[str, str]]] = {
            k: [tuple(x) for x in v] for k, v in (state.get("gb_children") or {}).items()
        }
        visited: set[str] = set(state.get("gb_visited") or [])

        known: dict[str, str] = {}
        for e in base:
            if e.url:
                known[_path_of(e.url)] = e.url
        for kids in children.values():
            for href, _ in kids:
                known.setdefault(_path_of(href), href)

        while True:
            frontier = [u for p, u in known.items() if p not in visited]
            if not frontier:
                break
            for i in range(0, len(frontier), GITBOOK_CRAWL_BATCH):
                chunk = frontier[i:i + GITBOOK_CRAWL_BATCH]
                sidebars = await scraper.gitbook_sidebars(chunk)
                for u in chunk:
                    visited.add(_path_of(u))
                    kids = _direct_children(sidebars.get(u, ""), u, root_url)
                    if kids:
                        children[_path_of(u)] = kids
                        for href, _ in kids:
                            known.setdefault(_path_of(href), href)
                if checkpoint:
                    await checkpoint.save_data({
                        "gb_children": {k: [list(t) for t in v] for k, v in children.items()},
                        "gb_visited": sorted(visited),
                    })
        return base, children

    @staticmethod
    def _assemble(base: list[TocEntry], children: dict, root_url: str) -> list[TocEntry]:
        """Stitch the root skeleton (top-level sections + their direct children)
        with each page's collected children into one DFS-ordered TocEntry list."""
        out: list[TocEntry] = []

        def emit_page(href: str, title: str, level: int, parent_url, seen: frozenset):
            out.append(TocEntry(title=title, url=href, level=level,
                                is_article=True, parent_url=parent_url))
            p = _path_of(href)
            if p in seen:  # cycle guard
                return
            for chref, ctitle in children.get(p, []):
                emit_page(chref, ctitle, level + 1, href, seen | {p})

        n = len(base)
        idx = 0
        while idx < n:
            node = base[idx]
            idx += 1
            if node.level != 0:
                continue
            if node.url:
                emit_page(node.url, node.title, 0, None, frozenset())
            else:
                # url-less section header; its level-1 children come from the root walk
                out.append(TocEntry(title=node.title, url=None, level=0,
                                    is_article=False, parent_url=None))
                while idx < n and base[idx].level == 1:
                    child = base[idx]
                    idx += 1
                    if child.url:
                        emit_page(child.url, child.title, 1, None, frozenset())
                    else:
                        out.append(TocEntry(title=child.title, url=None, level=1,
                                            is_article=False, parent_url=None))
        return out

    def content_config(self) -> dict:
        return {
            # No includeTags (onlyMainContent heuristic), so drop GitBook's
            # in-page chrome explicitly: the "Was this helpful?" widget and the
            # prev/next page footer navigation. No-op when absent.
            "excludeTags": [
                "[data-testid='page-feedback']",
                "[data-testid='page-footer-navigation']",
            ],
            "onlyMainContent": True,
            "waitFor": 3000,
        }


PROFILE = GitBookProfile()
registry.register(PROFILE)
