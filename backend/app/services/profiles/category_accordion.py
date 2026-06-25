"""Category-accordion help-center profile.

Some help centers publish their docs as one landing page per product, grouping
articles into collapsible "category section" blocks::

    <div class="category-section">
      <label><span>Get started</span></label>     ← section title (no URL)
      <div>
        <p><a href="/help/<cat>/article-a/">Article A</a></p>
        <p><a href="/help/<cat>/article-b/">Article B</a></p>
      </div>
    </div>

Section ``<label>``s have no link of their own, so they become structural
(url-less, ``is_article=False``) TOC headers; the articles inside are their
level-1 children, linked by level adjacency (same as the GitBook profile).

Each product tree lives under its own ``…/<product>-category/`` path and is
configured as a separate source. Articles are scoped to the source's own
``-category`` path, so the landing page's cross-product nav and any external
links can't leak into the TOC.

Detection: the publisher also serves marketing pages with generic CSS, so the
fingerprint pairs the publisher host with the ``category-section`` /
``category-sidebar`` help markup to avoid false positives on unrelated pages.
"""

from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from app.services.profiles import registry
from app.services.profiles.base import TocEntry

# Help center is served from this host; the same site's marketing pages reuse
# generic category CSS, which would otherwise false-positive.
_PUBLISHER_HOST = "keepit.com"


def _category_root(root_url: str) -> str:
    """Return the in-scope path prefix for *root_url*.

    For ``…/help/microsoft-365-category/article/`` this is
    ``/help/microsoft-365-category/`` — the path up to and including the segment
    ending in ``-category``. Falls back to the full path when no such segment
    exists.
    """
    parts = [p for p in urlparse(root_url).path.split("/") if p]
    for i, seg in enumerate(parts):
        if seg.endswith("-category"):
            return "/" + "/".join(parts[: i + 1]) + "/"
    path = urlparse(root_url).path
    return path if path.endswith("/") else path + "/"


class CategoryAccordionProfile:
    name = "category_accordion"

    def detect(self, root_html: str, root_url: str) -> bool:
        host = urlparse(root_url).netloc.lower()
        if _PUBLISHER_HOST not in host:
            return False
        return "category-section" in root_html or "category-sidebar" in root_html

    async def build_toc(self, root_url: str, scraper) -> list[TocEntry]:
        soup = BeautifulSoup(await scraper.get_html(root_url), "html.parser")
        scope = _category_root(root_url)
        out: list[TocEntry] = []
        seen: set[str] = set()

        def in_scope(href: str) -> str | None:
            url = urljoin(root_url, href)
            path = urlparse(url).path
            if not path.startswith(scope):
                return None
            if path.rstrip("/") == scope.rstrip("/"):
                return None  # the category landing page itself
            return url

        for sec in soup.select(".category-section"):
            label = sec.find("label")
            sec_title = label.get_text(strip=True) if label else ""

            articles: list[TocEntry] = []
            for a in sec.select("a[href]"):
                url = in_scope(a.get("href", ""))
                title = a.get_text(strip=True)
                if not url or not title or url in seen:
                    continue
                seen.add(url)
                articles.append(
                    TocEntry(title=title, url=url, level=1, is_article=True)
                )

            if not articles:
                continue
            if sec_title:
                out.append(
                    TocEntry(title=sec_title, url=None, level=0, is_article=False)
                )
            out.extend(articles)

        return out

    def content_config(self) -> dict:
        return {
            # The article body is <article class="m article">, but tables and
            # other embedded blocks render in sibling <div class="m embed">
            # *outside* the article — include both or tables get dropped. The
            # sidebar (also .m.embed) is removed again via excludeTags below.
            "includeTags": ["article.article", ".m.embed"],
            # Strip the page chrome that rides inside those containers: the
            # breadcrumb, the under-title byline, the bottom category chips, the
            # author box, the nav sidebar, and the related-articles block.
            "excludeTags": [
                ".m.breadcrumb",
                ".sub",
                ".tags",
                ".author",
                ".category-sidebar",
                ".m.related",
            ],
            "onlyMainContent": False,
            "waitFor": 1500,
        }


PROFILE = CategoryAccordionProfile()
registry.register(PROFILE)
