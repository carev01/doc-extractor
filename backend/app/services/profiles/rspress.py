"""Rspress static-docs profile (full nav tree + bodies pre-rendered into HTML).

Targets documentation sites built on the Rspress framework (rspress.dev), whose
entire sidebar nav AND article bodies are server-rendered into every page's
static HTML. After JS hydration Rspress collapses the sidebar to the current
page, so a rendered scrape sees only one nav link; the full tree exists only in
the raw HTML. Both the tree and the bodies are static, so the profile runs
entirely on the raw_http path (plain GET + local scoping, no render).

Validated against AvePoint Learn (learn.avepoint.com); keyed on the generic
Rspress fingerprint, not the vendor.
"""

import posixpath
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from app.services.profiles import registry
from app.services.profiles.base import TocEntry


class RspressProfile:
    name = "rspress"
    # Whole tree + bodies are static HTML; fetch directly, no render.
    content_engine = "raw_http"

    def detect(self, root_html: str, root_url: str) -> bool:
        # The sidebar nav hook plus the article hook together are distinctive to
        # Rspress; either alone is too generic.
        return "rspress-doc" in root_html and "rspress-sidebar" in root_html

    async def build_toc(self, root_url: str, scraper) -> list[TocEntry]:
        try:
            raw = await scraper.get_raw(root_url)
        except Exception:
            return []
        soup = BeautifulSoup(raw or "", "html.parser")
        side = soup.select_one("aside.rspress-sidebar")
        if not side:
            return []

        # The guide's top path segment (e.g. "/m365/") — used to exclude the
        # site logo ("/index.html") and links into other guides.
        root_segs = [s for s in urlparse(root_url).path.split("/") if s]
        top = "/" + root_segs[0] + "/" if root_segs else "/"

        # Pass 1: ordered nodes (section <h2> headers + in-guide menu anchors).
        # kind is "h2" (title only) or "a" (title + absolute url).
        nodes: list[tuple[str, str, str | None]] = []
        for el in side.find_all(["h2", "a"]):
            if el.name == "h2":
                title = el.get_text(strip=True)
                if title:
                    nodes.append(("h2", title, None))
                continue
            href = el.get("href")
            if not href:
                continue
            url = urljoin(root_url, href)
            if not urlparse(url).path.startswith(top):
                continue  # logo / cross-guide link
            nodes.append(("a", el.get_text(strip=True), url))

        # Guide root = longest common directory prefix of the article paths;
        # article level = path depth below it (0-based).
        art_dirs = [posixpath.dirname(urlparse(u).path) for k, _, u in nodes if k == "a"]
        guide_root = posixpath.commonpath(art_dirs) if art_dirs else top.rstrip("/")

        def article_level(url: str) -> int:
            rel = urlparse(url).path[len(guide_root):].lstrip("/")
            return len(rel.split("/")) - 1

        # Pass 2: emit in DOM order. A section sits one level above the next
        # article it introduces; articles take their URL-derived level.
        out: list[TocEntry] = []
        for i, (kind, title, url) in enumerate(nodes):
            if kind == "a":
                out.append(TocEntry(title=title, url=url, level=article_level(url),
                                    is_article=True))
            else:
                nxt = next((article_level(u) for k, _, u in nodes[i + 1:] if k == "a"), 0)
                out.append(TocEntry(title=title, url=None, level=max(0, nxt - 1),
                                    is_article=False))
        return out

    def content_config(self) -> dict:
        return {
            "includeTags": [".rspress-doc"],
            # Drop the right-rail "on this page" TOC, the prev/next + edit
            # footer, and any in-doc breadcrumb/nav.
            "excludeTags": [".rspress-local-toc-container", ".rspress-doc-footer", "nav"],
            "onlyMainContent": False,
        }


PROFILE = RspressProfile()
registry.register(PROFILE)
