"""Warm-up + chapter-book documentation portal.

Targets a documentation portal whose books are paginated **one page per
chapter**, sitting behind a WAF — currently Red Hat Documentation
(``docs.redhat.com``). Two things make it hard, both shared with
:mod:`warmup_listgroup`:

* **A WAF (Akamai) blocks our scraper egress.** A cold Firecrawl scrape (and a
  plain Browserless render) gets an Akamai "Access Denied" page. We defeat it
  with a **warm-up navigation** (visit the site root first so Akamai's JS sets
  its clearance cookies in the session, then load the target) — see
  :meth:`Scraper.warmup_render`. Firecrawl can't do that warm-up, so both TOC
  discovery *and* content scraping go through Browserless
  (``render_engine = "browserless"``).
* **It's a single page per chapter, not per topic.** A book's ``/html/<book>/``
  landing (``…/index``) lists each chapter as its own page; every chapter page
  is self-contained, with its sub-sections inline as ``#`` anchors (not separate
  URLs). The same book is also offered as a single ``/html-single/`` mega-page —
  we normalise to the per-chapter ``/html/`` form so each chapter is one article
  (the export splitter never breaks an article, so one giant html-single page
  would otherwise be un-splittable).

TOC: warm-up render the book index, read the chapter links out of the main
content column (``#main-content``) — they are the only same-book, non-anchor
links — in document order. The book is a flat list of chapters (level 0).

Content: a chapter's body is ``<article>`` (it carries the chapter ``<h1>`` and
every sub-section, and — unlike ``#main-content`` — excludes the global
navigation sidebar). Fetched via warm-up render, innerHTML markdownified.

Detection: cold root HTML is the WAF block page, so an HTML fingerprint is
useless; we match by the publisher host + ``/documentation/`` path instead.
"""

from urllib.parse import urldefrag, urljoin, urlparse

from bs4 import BeautifulSoup

from app.services.profiles import registry
from app.services.profiles.base import TocEntry

# Publisher host + path this profile targets; detection is host/path based
# because the cold HTML is a WAF block page (cf. warmup_listgroup → dell.com).
_PUBLISHER_HOST = "docs.redhat.com"
_DOC_PATH = "/documentation/"
_WARMUP_URL = "https://docs.redhat.com/en"
# The book index's main column lists the chapter pages; a chapter's body is the
# <article> (excludes the global nav sidebar that #main-content also contains).
_TOC_SELECTOR = "#main-content"
_CONTENT_SELECTOR = "article"


def _to_multipage(url: str) -> str:
    """Force the per-chapter multi-page ``/html/`` form (not ``/html-single/``)."""
    return url.replace("/html-single/", "/html/")


def _book_prefix(index_url: str) -> str:
    """The ``…/html/<book>/`` prefix that scopes a book's own chapter links."""
    base, _frag = urldefrag(index_url)
    if base.endswith("/index"):
        return base[: -len("index")]            # …/<book>/index -> …/<book>/
    if base.endswith("/"):
        return base
    return base.rsplit("/", 1)[0] + "/"          # …/<book>/foo -> …/<book>/


class WarmupBookProfile:
    name = "warmup_book"
    # WAF (Akamai) blocks Firecrawl; TOC and content both go through Browserless
    # with a warm-up navigation (see module docstring / warmup_listgroup).
    render_engine = "browserless"

    MAX_NODES = 2000  # safety bound on chapter count

    def detect(self, root_html: str, root_url: str) -> bool:
        """Match by URL — cold root HTML is the WAF block page, so an HTML
        fingerprint can't be relied on."""
        p = urlparse(root_url)
        return p.netloc.endswith(_PUBLISHER_HOST) and _DOC_PATH in p.path

    async def build_toc(self, root_url: str, scraper) -> list[TocEntry]:
        index_url = _to_multipage(root_url)
        prefix = _book_prefix(index_url)
        try:
            data = await scraper.warmup_render(
                index_url, selector=_TOC_SELECTOR, warmup_url=_WARMUP_URL
            )
        except Exception:
            return []
        html = (data or {}).get("outerHtml") or ""
        soup = BeautifulSoup(html, "html.parser")

        out: list[TocEntry] = []
        seen: set[str] = set()
        for a in soup.select("a[href]"):
            if len(out) >= self.MAX_NODES:
                break
            resolved = urljoin(index_url, a.get("href", "").strip())
            # Same-book chapter pages only. A chapter link is fragment-less; a
            # sub-section link (e.g. ...oadp-...#oadp-features in the global nav)
            # is skipped outright rather than defragged — defragging would collide
            # it with the real chapter URL and, appearing first, steal its title.
            if "#" in resolved:
                continue
            if not resolved.startswith(prefix) or resolved in (prefix, prefix + "index"):
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            base = resolved
            title = a.get_text(strip=True)
            out.append(TocEntry(
                title=title or base, url=base, level=0,
                is_article=True, parent_url=None,
            ))
        return out

    def browserless_content_spec(self) -> dict:
        """Content path: warm up past the WAF, then take the innerHTML of the
        chapter ``<article>``, dropping page chrome — the ``nav.pagination``
        PreviousNext footer and the per-heading ``.copy-link-tooltip`` copy
        widgets ("Copy link / Link copied to clipboard!")."""
        return {
            "selector": _CONTENT_SELECTOR,
            "warmup_url": _WARMUP_URL,
            "excludeTags": ["nav.pagination", ".copy-link-tooltip"],
        }

    def content_config(self) -> dict:
        # Unused for content (render_engine=browserless), but kept for parity.
        return {
            "includeTags": [_CONTENT_SELECTOR],
            "onlyMainContent": False,
            "waitFor": 4000,
        }


PROFILE = WarmupBookProfile()
registry.register(PROFILE)
