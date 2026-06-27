"""Static docs site with a heading-grouped sidebar nav.

Targets the shared cloud-native "project documentation" Jekyll theme used by
Velero and sibling projects (Contour, Sonobuoy, …): a fully static site whose
left sidebar is a single ``<nav class="navigation">`` containing a flat sequence
of ``<h3>`` group headings, each followed by a ``<ul>`` of article links — no
nesting beyond that one level — and whose article body lives in a single
``.documentation-container``. The name is the generic mechanism (a nav grouped
by headings), not the vendor.

Both the tree and the bodies are static HTML, so the whole thing runs on the
raw_http path (a plain GET + local scoping, no render):

* TOC: GET the landing page once, read ``nav.navigation`` — emit each ``<h3>``
  as a url-less section header and each following ``<ul>``'s links as articles.
  A version ``.dropdown`` sits in the same sidebar column but *outside*
  ``nav.navigation``, so scoping to the nav drops the version selector for free.
* Content: ``.documentation-container``; the right-rail "on this page" mini-TOC
  (``nav#TableOfContents``) is dropped (see content_config).

A source points at a version landing (``/docs/<version>/``, e.g.
``/docs/v1.18/``); links outside that version prefix (other versions, external)
are skipped, and same-page anchor duplicates are de-duped.
"""

import re
from urllib.parse import urljoin, urlparse, urldefrag

from bs4 import BeautifulSoup

from app.services.profiles import registry
from app.services.profiles.base import TocEntry

# /docs/<version>[/...] — the per-version path prefix that scopes the crawl.
_VERSION_PATH_RE = re.compile(r"(/docs/[^/?#]+)")


class GroupedNavProfile:
    name = "grouped_nav"
    # Tree and bodies are both static HTML; fetch directly, no render.
    content_engine = "raw_http"

    MAX_NODES = 5000  # safety bound on tree size

    def detect(self, root_html: str, root_url: str) -> bool:
        # The content wrapper class plus the heading-grouped nav together are
        # distinctive to this theme; either alone is too generic.
        return 'documentation-container' in root_html and 'class="navigation"' in root_html

    async def build_toc(self, root_url: str, scraper) -> list[TocEntry]:
        try:
            html = await scraper.get_raw(root_url)
        except Exception:
            return []
        soup = BeautifulSoup(html or "", "html.parser")
        nav = soup.select_one("nav.navigation")
        if nav is None:
            return []

        p = urlparse(root_url)
        origin = f"{p.scheme}://{p.netloc}"
        m = _VERSION_PATH_RE.match(p.path)
        # Scope to this version's subtree; fall back to /docs/ if the path is odd.
        version_path = (m.group(1) if m else "/docs").rstrip("/") + "/"
        prefix = origin + version_path

        out: list[TocEntry] = []
        seen: set[str] = set()

        for el in nav.find_all(["h3", "ul"], recursive=False):
            if len(out) >= self.MAX_NODES:
                break
            if el.name == "h3":
                title = el.get_text(strip=True)
                if title:
                    out.append(TocEntry(
                        title=title, url=None, level=0,
                        is_article=False, parent_url=None,
                    ))
                continue
            # <ul> of article links following a heading.
            for a in el.select("a[href]"):
                if len(out) >= self.MAX_NODES:
                    break
                resolved = urljoin(root_url, a.get("href", "").strip())
                base, _frag = urldefrag(resolved)
                if not base.startswith(prefix):
                    continue  # other version / external link
                # Canonicalise to the trailing-slash form Velero serves (avoids a
                # 301 hop) and de-dupe anchor variants of the same page.
                if "?" not in base and not base.endswith("/"):
                    base += "/"
                if base in seen:
                    continue
                seen.add(base)
                title = a.get_text(strip=True)
                out.append(TocEntry(
                    title=title or base, url=base, level=1,
                    is_article=True, parent_url=None,
                ))
        return out

    def content_config(self) -> dict:
        return {
            "includeTags": [".documentation-container"],
            # Drop the right-rail "on this page" mini-TOC; the rest is prose.
            "excludeTags": ["#TableOfContents"],
            "onlyMainContent": False,
            "waitFor": 1500,
        }


PROFILE = GroupedNavProfile()
registry.register(PROFILE)
