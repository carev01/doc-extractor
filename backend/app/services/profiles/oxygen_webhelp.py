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
        node: dict[str, dict] = {}          # tocid -> {url, title}
        parent_of: dict[str, str] = {}      # tocid -> parent tocid
        children: dict[str, list[str]] = {} # tocid -> ordered child tocids (longest seen)
        top_order: list[str] = []           # ordered top-level tocids (longest seen)

        def tocid(li):
            if li.get("data-tocid"):
                return li["data-tocid"]
            d = li.find(attrs={"data-tocid": True})
            return d["data-tocid"] if d else None

        def direct_child_items(container):
            # treeitem <li> that are this container's nearest treeitem descendants
            out = []
            for ul in container.find_all("ul", recursive=False):
                out.extend([li for li in ul.find_all("li", recursive=False)
                            if li.get("role") == "treeitem"])
            return out

        for page_url, frag in fragments:
            soup = BeautifulSoup(frag or "", "html.parser")
            navs = soup.select("#wh_publication_toc") or [soup]
            nav = navs[0]
            # top-level
            tops = [t for li in direct_child_items(nav) for t in [tocid(li)] if t]
            if len(tops) > len(top_order):
                top_order = tops
            for li in nav.find_all("li", attrs={"role": "treeitem"}):
                tid = tocid(li)
                if not tid:
                    continue
                a = li.find("a", href=True)
                if a is not None and tid not in node:
                    node[tid] = {
                        "url": urldefrag(urljoin(page_url, a["href"]))[0],
                        "title": a.get_text(strip=True) or a["href"],
                    }
                pli = li.find_parent("li", attrs={"role": "treeitem"})
                ptid = tocid(pli) if pli is not None else None
                if ptid and tid not in parent_of:
                    parent_of[tid] = ptid
                kids = [t for c in direct_child_items(li) for t in [tocid(c)] if t]
                if len(kids) > len(children.get(tid, [])):
                    children[tid] = kids

        if not node:
            return []
        out: list[TocEntry] = []
        seen: set[str] = set()

        def walk(tid: str, level: int, parent_url):
            if tid in seen or tid not in node:
                return
            seen.add(tid)
            n = node[tid]
            out.append(TocEntry(title=n["title"], url=n["url"], level=level,
                                is_article=True, parent_url=parent_url))
            for c in children.get(tid, []):
                walk(c, level + 1, n["url"])

        roots = top_order or [t for t in node if t not in parent_of]
        for t in roots:
            walk(t, 0, None)
        for t in node:               # any unreached nodes → append at top
            if t not in seen:
                walk(t, 0, None)
        return out

    def content_config(self) -> dict:
        return {
            "includeTags": ["article"],
            "excludeTags": [".related-links", "nav", "header", "footer", ".wh_breadcrumb"],
            "onlyMainContent": False,
        }


PROFILE = OxygenWebhelpProfile()
registry.register(PROFILE)
