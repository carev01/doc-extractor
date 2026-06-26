"""Sphinx (ReadTheDocs theme) documentation profile.

Targets Sphinx output rendered with the ``sphinx_rtd_theme`` (the ``wy-nav-side``
/ ``wy-menu-vertical`` layout, e.g. Bacula Enterprise docs). Pages are fully
server-rendered static HTML, so content is fetched directly (raw_http) and scoped
to the Sphinx topic body ``[role=main]``.

TOC — why a crawl:
    The RTD sidebar shows every top-level section but only expands the *current*
    page's branch, and the home page's master ``toctree`` is usually ``:hidden:``
    (rendered into the sidebar, not the page body). So no single page carries the
    full tree. Each section/topic page does render its **direct children** as a
    ``.toctree-wrapper`` link list in its body, so we reconstruct the whole tree
    by crawling: seed from the home sidebar's top-level (``toctree-l1``) sections,
    then follow each page's direct ``.toctree-wrapper`` children breadth-first
    (bounded concurrency over fast GETs), and finally assemble the ordered tree
    depth-first so the curated reading order and nesting are preserved.

    The crawl is checkpointed (seeds / children map / fetched set) so a large
    site (Bacula: ~1700 pages, depth 9) resumes after an interruption instead of
    restarting.
"""

import asyncio
import logging
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from app.services.profiles import registry
from app.services.profiles.base import TocEntry
from app.services.profiles.content_scope import scope_content_html

logger = logging.getLogger(__name__)

# Pages fetched concurrently per BFS step (fast static GETs).
SPHINX_CRAWL_BATCH = 16
# Safety bound on tree size (Bacula ~1700; allow generous headroom).
MAX_NODES = 50000


def _strip_fragment(url: str) -> str:
    return url.split("#", 1)[0]


def _direct_children(html: str, base_url: str, host: str) -> list[tuple[str, str]]:
    """Ordered (url, title) of a page's direct ``.toctree-wrapper`` children.

    Only the first level of each toctree is taken (its ``<ul>``'s direct
    ``<li>``); deeper nesting (when a toctree uses ``:maxdepth: > 1``) is reached
    by recursing into each child's own page, so we never double-count or
    mis-level. Off-site and in-page (``#``) links are dropped.
    """
    soup = BeautifulSoup(html, "html.parser")
    main = soup.select_one("[role=main]") or soup.select_one(".document")
    if not main:
        return []
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for wrap in main.select(".toctree-wrapper"):
        ul = wrap.find("ul")
        if not ul:
            continue
        for li in ul.find_all("li", recursive=False):
            a = li.find("a", href=True)
            if not a:
                continue
            href = a["href"]
            if href.startswith("#"):
                continue
            absu = _strip_fragment(urljoin(base_url, href))
            if urlparse(absu).netloc != host or absu in seen:
                continue
            seen.add(absu)
            out.append((absu, a.get_text(strip=True)))
    return out


class SphinxProfile:
    name = "sphinx"
    # Topic bodies are static server-rendered HTML scoped by [role=main]; fetch
    # directly (the generic raw_http scoper uses this profile's content_config).
    content_engine = "raw_http"

    def detect(self, root_html: str, root_url: str) -> bool:
        # The sphinx_rtd_theme markers (wy-nav layout). Sphinx core markers alone
        # (.toctree-wrapper / role=main) aren't enough — other Sphinx themes lay
        # out the nav differently and this profile's seed step is RTD-specific.
        return "wy-nav-side" in root_html or "wy-menu-vertical" in root_html or \
            "sphinx_rtd_theme" in root_html

    async def build_toc(self, root_url: str, scraper) -> list[TocEntry]:
        host = urlparse(root_url).netloc
        checkpoint = getattr(scraper, "checkpoint", None)
        state = await checkpoint.load() if checkpoint else {}

        # ── Seeds: the home sidebar's top-level (toctree-l1) sections, in order.
        seeds: list[list] = state.get("sphinx_seeds")
        if seeds is None:
            root_html = await scraper.get_raw(root_url)
            soup = BeautifulSoup(root_html, "html.parser")
            nav = soup.select_one(".wy-menu-vertical")
            seeds = []
            if nav:
                for a in nav.select("li.toctree-l1 > a.reference.internal[href]"):
                    href = a.get("href", "")
                    if href.startswith("#"):
                        continue
                    seeds.append([
                        _strip_fragment(urljoin(root_url, href)),
                        a.get_text(strip=True),
                    ])
            if checkpoint:
                await checkpoint.save_data({"sphinx_seeds": seeds})
        if not seeds:
            return []

        # ── BFS: collect each page's direct children into a children map.
        children: dict[str, list[list]] = {
            k: [list(t) for t in v]
            for k, v in (state.get("sphinx_children") or {}).items()
        }
        fetched: set[str] = set(state.get("sphinx_fetched") or [])

        known: dict[str, str] = {u: t for u, t in seeds}
        for kids in children.values():
            for cu, ct in kids:
                known.setdefault(cu, ct)

        while len(fetched) < MAX_NODES:
            frontier = [u for u in known if u not in fetched]
            if not frontier:
                break
            for i in range(0, len(frontier), SPHINX_CRAWL_BATCH):
                chunk = frontier[i:i + SPHINX_CRAWL_BATCH]
                pages = await asyncio.gather(
                    *(self._safe_get(scraper, u) for u in chunk)
                )
                for u, html in zip(chunk, pages):
                    fetched.add(u)
                    kids = _direct_children(html, u, host) if html else []
                    if kids:
                        children[u] = [[cu, ct] for cu, ct in kids]
                        for cu, ct in kids:
                            known.setdefault(cu, ct)
                if checkpoint:
                    await checkpoint.save_data({
                        "sphinx_children": children,
                        "sphinx_fetched": sorted(fetched),
                    })

        # ── Assemble depth-first so curated order + nesting are preserved.
        out: list[TocEntry] = []
        visited: set[str] = set()

        def emit(url: str, title: str, level: int) -> None:
            if url in visited or len(out) >= MAX_NODES:
                return
            visited.add(url)
            out.append(TocEntry(
                title=title or url, url=url, level=level,
                is_article=True, parent_url=None,
            ))
            for cu, ct in children.get(url, []):
                emit(cu, ct, level + 1)

        for u, t in seeds:
            emit(u, t, 0)
        return out

    @staticmethod
    async def _safe_get(scraper, url: str) -> str:
        try:
            return await scraper.get_raw(url) or ""
        except Exception:
            return ""

    def content_config(self) -> dict:
        return {
            # Sphinx topic body. Drop the in-body child-link toctree (redundant
            # nav — hierarchy is captured in the TOC), heading anchor links, and
            # the RTD breadcrumb / prev-next / version chrome.
            "includeTags": ["[role=main]"],
            "excludeTags": [
                ".toctree-wrapper", ".headerlink", ".wy-breadcrumbs",
                ".rst-footer-buttons", ".rst-versions",
            ],
            "onlyMainContent": False,
            "waitFor": 1500,
        }

    def extract_content_html(self, raw_html: str, url: str) -> str | None:
        """Scope the topic body, then strip two recurring bits of page chrome the
        ``content_config`` CSS selectors can't target:

          - the "You can download this article as a PDF" note box — a normal
            ``.admonition`` (so it can't be excluded wholesale without dropping
            real notes), identified by the ``a.pdflink`` download link inside it;
          - the trailing "Go back to: <parent>" navigation paragraph — a bare
            ``<p>`` with no class, identifiable only by its text.

        Everything else (image absolutising, the content_config include/exclude
        scoping) is delegated to the shared ``scope_content_html``.
        """
        cfg = self.content_config()
        scoped = scope_content_html(
            raw_html, url, cfg["includeTags"], cfg.get("excludeTags") or []
        )
        if not scoped:
            return None
        soup = BeautifulSoup(scoped, "html.parser")

        # Drop the "download as PDF" note box (keep genuine notes).
        for a in soup.select("a.pdflink"):
            box = a.find_parent(class_="admonition") or a.find_parent("blockquote") or a.parent
            if box is not None:
                box.decompose()
        # The PDF note sits in a wrapping <blockquote>; remove any left empty.
        for bq in soup.find_all("blockquote"):
            if not bq.get_text(strip=True):
                bq.decompose()

        # Drop the trailing "Go back to: …" breadcrumb paragraph.
        for p in soup.find_all("p"):
            if p.get_text(strip=True).lower().startswith("go back to"):
                p.decompose()

        inner = soup.decode_contents().strip()
        return inner or None


PROFILE = SphinxProfile()
registry.register(PROFILE)
