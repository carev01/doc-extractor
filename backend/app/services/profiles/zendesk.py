"""Zendesk Help Center documentation profile.

Zendesk Help Center themes render the article tree client-side and gate the
public HTML behind bot protection (a plain GET of an ``/hc/…`` page returns 403),
but every Help Center exposes an open, paginated REST API that needs no auth:

    GET /api/v2/help_center/<locale>/categories/<id>/sections.json
    GET /api/v2/help_center/<locale>/categories/<id>/articles.json
    GET /api/v2/help_center/<locale>/articles/<id>.json   → {"article": {"body": …}}

A source points at one category (``/hc/<locale>/categories/<id>``). We build the
ordered tree from its sections (curated by ``position``, nested via
``parent_section_id``) and articles (grouped by ``section_id``, ordered by
``position``); section nodes are url-less structural headers (their landing
pages aren't articles), articles carry the public ``html_url`` for display while
``content_url`` points at the article API so the raw_http path can fetch the
body as JSON — unwrapped by :meth:`extract_content_html`.
"""

import json
import re
from urllib.parse import urljoin, urlparse

from app.services.profiles import registry
from app.services.profiles.base import TocEntry

# /hc/<locale>/categories/<id> — the canonical Help Center category URL.
_CATEGORY_RE = re.compile(r"/hc/([^/]+)/categories/(\d+)")


class ZendeskProfile:
    name = "zendesk"
    # Article bodies come from the JSON API (extract_content_html unwraps the
    # "body" field), fetched per-entry via content_url — see module docstring.
    content_engine = "raw_http"

    PER_PAGE = 100
    MAX_PAGES = 200  # safety bound (× PER_PAGE = 20k items)

    def detect(self, root_html: str, root_url: str) -> bool:
        # The /hc/<locale>/categories|sections|articles/ path scheme is unique to
        # Zendesk Help Center and is reliable even when the page itself is
        # bot-gated (so root_html may be a 403 shell). Also accept clear in-page
        # markers for the rare non-category landing.
        if re.search(r"/hc/[^/]+/(categories|sections|articles)/\d+", root_url):
            return True
        lower = root_html.lower()
        return "/api/v2/help_center/" in lower or "zendesk" in lower

    async def build_toc(self, root_url: str, scraper) -> list[TocEntry]:
        m = _CATEGORY_RE.search(root_url)
        if not m:
            return []
        locale, cat_id = m.group(1), m.group(2)
        api = f"{urlparse(root_url).scheme}://{urlparse(root_url).netloc}/api/v2/help_center/{locale}"

        sections = await self._fetch_all(
            scraper, f"{api}/categories/{cat_id}/sections.json", "sections"
        )
        articles = await self._fetch_all(
            scraper, f"{api}/categories/{cat_id}/articles.json", "articles"
        )
        if not sections and not articles:
            return []

        # Articles grouped under their section, in curated order.
        arts_by_section: dict[int, list] = {}
        for a in articles:
            arts_by_section.setdefault(a.get("section_id"), []).append(a)
        for lst in arts_by_section.values():
            lst.sort(key=lambda a: (a.get("position", 0), a.get("title", "")))

        # Section hierarchy: children grouped by parent_section_id, by position.
        children: dict[int | None, list] = {}
        for s in sections:
            children.setdefault(s.get("parent_section_id"), []).append(s)
        for lst in children.values():
            lst.sort(key=lambda s: (s.get("position", 0), s.get("name", "")))

        out: list[TocEntry] = []

        def emit_article(a: dict, level: int, parent_url: str | None) -> None:
            html_url = a.get("html_url")
            aid = a.get("id")
            if not html_url or aid is None:
                return
            out.append(TocEntry(
                title=(a.get("title") or "").strip() or html_url,
                url=html_url, level=level, is_article=True, parent_url=parent_url,
                content_url=f"{api}/articles/{aid}.json",
            ))

        def walk_sections(parent_section_id, level: int) -> None:
            for s in children.get(parent_section_id, []):
                sid = s.get("id")
                # Section landing pages aren't articles -> url-less structural
                # header (never scraped; children attach by level adjacency).
                out.append(TocEntry(
                    title=(s.get("name") or "").strip(), url=None,
                    level=level, is_article=False, parent_url=None,
                ))
                for a in arts_by_section.get(sid, []):
                    emit_article(a, level + 1, None)
                walk_sections(sid, level + 1)

        walk_sections(None, 0)
        # Articles with no section (rare) land at the end, top level.
        for a in arts_by_section.get(None, []):
            emit_article(a, 0, None)
        return out

    async def _fetch_all(self, scraper, url: str, key: str) -> list:
        """Fetch every page of a paginated Help Center collection. Returns [] on
        any failure (so a category that exposes only one of sections/articles, or
        a transient error, degrades gracefully rather than aborting the tree)."""
        out: list = []
        page = 1
        while page <= self.MAX_PAGES:
            sep = "&" if "?" in url else "?"
            try:
                raw = await scraper.get_raw(f"{url}{sep}per_page={self.PER_PAGE}&page={page}")
                data = json.loads(raw)
            except Exception:
                break
            items = data.get(key) or []
            out.extend(items)
            if not data.get("next_page"):
                break
            page += 1
        return out

    def extract_content_html(self, raw: str, url: str) -> str | None:
        """Unwrap the article ``body`` HTML from the article API JSON."""
        try:
            data = json.loads(raw)
        except Exception:
            return None
        body = (data.get("article") or {}).get("body") if isinstance(data, dict) else None
        if not body:
            return None
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(body, "html.parser")
        for img in soup.find_all("img"):
            src = img.get("src")
            if src:
                img["src"] = urljoin(url, src)
        return str(soup)

    def content_config(self) -> dict:
        # Unused on the raw_http path (extract_content_html handles bodies), but
        # present for interface completeness / any fallback.
        return {"onlyMainContent": True, "waitFor": 1500}


PROFILE = ZendeskProfile()
registry.register(PROFILE)
