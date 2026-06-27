"""Static docs site with the full nav tree pre-rendered into every page.

Targets a documentation platform whose **entire** nested table of contents is
server-rendered into the static HTML of every page (no lazy mounting, no
contextual sidebar, no client expansion) — currently the Veeam Help Center
(``helpcenter.veeam.com/docs/<product>/...``), the same engine across every
Veeam product guide. The name is the generic mechanism (a pre-rendered TOC
tree), not the vendor.

Because the whole ordered tree is in the page HTML and the bodies are static,
the profile runs entirely on the raw_http path (a plain GET + local scoping, no
render):

* TOC: GET any page once, parse the nested ``<ul>/<li><a>`` tree under the
  sidebar (``.page-toc .page-toc__search-links`` — the search box filters this
  same list client-side, so it *is* the full nav). Parent topics carry their own
  ``href`` (they are real landing pages as well as section parents), so they are
  kept with a URL and scraped; leaves are articles. Order is DOM order.
* Content: the topic body is ``article.js-page-article`` (h1 + body); the
  right-rail "In this article" mini-TOC (``.mini-toc__container``) and the
  per-page "Page updated… / Send feedback" ``<footer>`` are dropped.

A source points at any guide page (e.g. ``.../userguide/overview.html``); the
whole product guide is extracted (relative hrefs resolve against that page).
"""

from app.services.profiles import registry
from app.services.profiles.base import TocEntry
from app.services.profiles.strategies import parse_sidebar_tree

# The sidebar nav list. Its class says "search-links" because the TOC search box
# filters this very list — it holds the complete pre-rendered tree.
_NAV_SELECTOR = ".page-toc .page-toc__search-links"


class PrerenderedTocProfile:
    name = "prerendered_toc"
    # Whole tree + bodies are static HTML; fetch directly, no render.
    content_engine = "raw_http"

    def detect(self, root_html: str, root_url: str) -> bool:
        # The sidebar nav hook plus the article hook together are distinctive to
        # this theme; either alone is too generic.
        return "js-page-toc" in root_html and "js-page-article" in root_html

    async def build_toc(self, root_url: str, scraper) -> list[TocEntry]:
        try:
            html = await scraper.get_raw(root_url)
        except Exception:
            return []
        return parse_sidebar_tree(html or "", root_url, _NAV_SELECTOR)

    def content_config(self) -> dict:
        return {
            "includeTags": ["article.js-page-article"],
            # Drop the right-rail mini-TOC and the page-updated/feedback footer.
            "excludeTags": [".mini-toc__container", "footer"],
            "onlyMainContent": False,
            "waitFor": 1500,
        }


PROFILE = PrerenderedTocProfile()
registry.register(PROFILE)
