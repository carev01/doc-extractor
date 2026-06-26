"""Helpjuice knowledge-base documentation profile.

Helpjuice renders its category sidebar client-side and lazy-loads each node, but
every category/section exposes an open JSON endpoint that needs no auth:

    GET /en_US/<slug>.json  ->  {
        "name": ...,
        "children":  [{"name", "url", "children?"}, ...],   # sub-sections
        "published_questions": [{"name", "url", "position"}, ...],  # articles
    }

So the whole ordered tree is built from JSON alone — no rendering, no HTML
scrape for the TOC. We start at the source's own node and walk **down** only,
which naturally scopes the extraction to that node's subtree (e.g. a single
product section out of a multi-product knowledge base) without pulling in its
siblings.

  - Sub-sections (``children``) are url-less structural headers (their landing
    pages are just lists of links); we recurse into each via its own ``.json``.
  - Articles (``published_questions``) are the real content pages; they carry
    the public URL and are scraped via the raw_http path.

Article bodies are served as static HTML in ``<article class="article">`` (a
Froala ``.fr-view`` body plus a title header), so content uses the generic
raw_http scoper with ``includeTags=["article.article"]``; the author byline /
"Updated at" chrome in the header and the empty tag list are dropped via
``excludeTags`` (see :meth:`content_config`).
"""

import json
from urllib.parse import urlparse, urlunparse

from app.services.profiles import registry
from app.services.profiles.base import TocEntry


class HelpjuiceProfile:
    name = "helpjuice"
    # Article pages are fully server-rendered static HTML scoped by
    # article.article (see content_config); fetch them directly rather than
    # rendering. The generic scoper in _scrape_via_raw_http uses this config.
    content_engine = "raw_http"

    # Safety bound: stop walking after this many section nodes so a malformed or
    # cyclic tree can never spin forever (the visited-set already guards cycles).
    MAX_NODES = 2000

    def detect(self, root_html: str, root_url: str) -> bool:
        # data-helpjuice-* attributes are emitted on every Helpjuice themed
        # element and are unique to the platform (no collision with zendesk /
        # freshdesk / intercom, which use their own markers).
        return "data-helpjuice-" in root_html

    async def build_toc(self, root_url: str, scraper) -> list[TocEntry]:
        out: list[TocEntry] = []
        visited: set[str] = set()
        await self._walk(scraper, self._json_url(root_url), level=0, out=out, visited=visited)
        return out

    async def _walk(self, scraper, node_json_url: str, level: int,
                    out: list[TocEntry], visited: set[str]) -> None:
        if node_json_url in visited or len(visited) >= self.MAX_NODES:
            return
        visited.add(node_json_url)

        try:
            data = json.loads(await scraper.get_raw(node_json_url))
        except Exception:
            # A node whose JSON fails to load degrades gracefully: its subtree is
            # skipped, the rest of the tree is still built.
            return
        if not isinstance(data, dict):
            return

        # This node's own articles, in curated order.
        questions = sorted(
            data.get("published_questions") or [],
            key=lambda q: (q.get("position", 0), q.get("name", "")),
        )
        for q in questions:
            url = self._clean(q.get("url"))
            if not url:
                continue
            out.append(TocEntry(
                title=(q.get("name") or "").strip() or url,
                url=url, level=level, is_article=True, parent_url=None,
            ))

        # Sub-sections, in curated order; recurse into each.
        children = sorted(
            data.get("children") or [],
            key=lambda c: (c.get("position", 0), c.get("name", "")),
        )
        for child in children:
            child_page_url = self._clean(child.get("url"))
            if not child_page_url:
                continue
            # Section landing pages are link lists, not articles -> url-less
            # structural header (never scraped; children attach by level).
            out.append(TocEntry(
                title=(child.get("name") or "").strip(), url=None,
                level=level, is_article=False, parent_url=None,
            ))
            await self._walk(
                scraper, self._json_url(child_page_url), level + 1, out, visited
            )

    @staticmethod
    def _clean(url: str | None) -> str:
        """Strip the ``?kb_language=…`` (and any other) query/fragment from a
        Helpjuice URL, leaving the canonical page URL."""
        if not url:
            return ""
        p = urlparse(url)
        return urlunparse((p.scheme, p.netloc, p.path, "", "", ""))

    @classmethod
    def _json_url(cls, page_url: str) -> str:
        """Map a category/section page URL to its ``.json`` endpoint.

        Helpjuice serves a node's JSON at ``/en_US/<slug>.json``. Section URLs
        are flat single-segment slugs; the slug may itself be prefixed with the
        node id (e.g. the product root ``115000502027-Axcient-x360Recover``).
        The localized ``/en_US/`` segment is normalized in (some URLs from the
        API omit it).
        """
        p = urlparse(cls._clean(page_url))
        segs = [s for s in p.path.split("/") if s and s != "en_US"]
        slug = "/".join(segs)
        return f"{p.scheme}://{p.netloc}/en_US/{slug}.json"

    def content_config(self) -> dict:
        return {
            "includeTags": ["article.article"],
            # Drop the title header's author byline ("Written By … / Updated at
            # …") and the empty tag list; keep the article title + Froala body.
            "excludeTags": [
                '[data-helpjuice-element="Author Profile Header"]',
                '[data-helpjuice-element="Article Author Details"]',
                ".tags",
            ],
            "onlyMainContent": False,
            "waitFor": 1500,
        }


PROFILE = HelpjuiceProfile()
registry.register(PROFILE)
