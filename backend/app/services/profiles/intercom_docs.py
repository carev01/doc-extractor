"""Intercom Help Center, newer "Docs" template (e.g. help.druva.com since late 2026).

Intercom re-skinned help centers onto a Next.js "Docs" layout (``DocsHome`` /
``DocsCollection`` / ``DocsArticle`` CSS modules with hashed suffixes). None of
the classic markup the ``intercom`` profile reads survives — no
``a.collection-link``, no ``data-testid="article-link"`` — so Druva's TOC fell to
the synthetic Index page and the collapse guard aborted the run.

What the template does carry is the **whole** collection tree, embedded in every
page's ``__NEXT_DATA__`` at ``props.pageProps.v2Navbar.collections``: each
collection has ``articles`` (``title`` + ``url``) and nested ``subcollections``.
That is the TOC — one GET, no crawl, and every level (the classic hub-and-spoke
crawl only ever reached a collection's direct articles: 636 of Druva's 3,359).

* Collections are listing pages, so they become URL-less headings; a
  collection's own articles come before its subcollections, as the site lists
  them. An article filed in two collections appears twice here and keeps its
  first position (``_dedupe_toc_entries``).
* Content is server-rendered: the ``h1`` title plus the
  ``article.DocsArticleBody_body`` block. Breadcrumbs, the reactions widget,
  related articles and the on-page TOC sit outside it. Classes are matched by
  prefix (``[class*=...]``) because the suffix is a per-build hash.
"""

import json
import logging
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from app.services.profiles import registry
from app.services.profiles.base import TocEntry

logger = logging.getLogger(__name__)

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)
_TITLE_SELECTOR = 'h1[class*="DocsArticle_title"]'
_BODY_SELECTOR = 'article[class*="DocsArticleBody_body"]'


def _nav_collections(html: str) -> list[dict] | None:
    m = _NEXT_DATA_RE.search(html or "")
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
        collections = data["props"]["pageProps"]["v2Navbar"]["collections"]
    except (ValueError, KeyError, TypeError):
        return None
    return collections if isinstance(collections, list) else None


def _walk(collection: dict, level: int, out: list[TocEntry]) -> None:
    name = (collection.get("name") or "").strip()
    if name:
        out.append(TocEntry(title=name, url=None, level=level, is_article=False))
    child = level + 1 if name else level
    for art in collection.get("articles") or []:
        url = art.get("url")
        if url:
            out.append(TocEntry(title=(art.get("title") or url).strip(), url=url,
                                level=child, is_article=True))
    for sub in collection.get("subcollections") or []:
        _walk(sub, child, out)


class IntercomDocsProfile:
    name = "intercom_docs"
    # Bodies are server-rendered; fetch directly, no render.
    content_engine = "raw_http"
    raw_http_concurrency = 4

    def detect(self, root_html: str, root_url: str) -> bool:
        return "intercom" in root_html and (
            "docs-home-collection-card" in root_html
            or "DocsArticleBody_body" in root_html
        )

    async def build_toc(self, root_url: str, scraper) -> list[TocEntry]:
        try:
            html = await scraper.get_raw(root_url)
        except Exception:
            return []
        collections = _nav_collections(html)
        if not collections:
            logger.warning("intercom_docs: no v2Navbar collections in %s", root_url)
            return []
        out: list[TocEntry] = []
        for c in collections:
            _walk(c, 0, out)
        return out

    def extract_content_html(self, raw: str, url: str) -> str | None:
        """Title + article body with absolute image URLs; None without a body."""
        soup = BeautifulSoup(raw, "html.parser")
        body = soup.select_one(_BODY_SELECTOR)
        if body is None:
            return None
        for img in body.find_all("img"):
            src = img.get("src")
            if src:
                img["src"] = urljoin(url, src)
        title = soup.select_one(_TITLE_SELECTOR)
        return (str(title) if title else "") + str(body)

    def content_config(self) -> dict:
        # Unused on the raw_http path (extract_content_html scopes), but
        # extract_source always calls content_config(), so it must exist.
        return {
            "includeTags": [_TITLE_SELECTOR, _BODY_SELECTOR],
            "onlyMainContent": False,
            "waitFor": 1500,
        }


PROFILE = IntercomDocsProfile()
registry.register(PROFILE)
