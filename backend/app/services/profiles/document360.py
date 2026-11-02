"""Document360 knowledge-base profile.

Document360 sites (e.g. Securiti's ``helpcenter.securiti.ai/docs``) are Angular
single-page apps whose article pages are server-side rendered. We build the
ordered TOC from the site's navigation tree (no per-page crawl) and fetch each
article body over the fast raw-HTTP path, scoping to the rendered
``#articleContent`` container.

The tree comes from the reader app's own sidebar call,
``GET /<siteApiPath>/document/get-category-tree`` (same session cookies). Older
reader builds instead embedded it as Angular TransferState JSON in a
``<script id="serverApp-state" type="application/json">`` tag, which stays the
fallback. The site build of 2026-11-01 stopped embedding it: the only tree left
in the state was the landing article's own index of its siblings, and taking
that for the nav collapsed Securiti's 2,535-page TOC to 19 entries.

Tree shape (the API's ``result``, or ``…result.categories`` in TransferState): a
tree of nodes linked by a nested ``children`` array, each node carrying::

    id, title, slug, order, categoryType, articleType, isHidden, children

- ``articleType == 0``            → an article (leaf content page).
- ``articleType is None``         → a category/folder.
    - with a ``slug``  → a "page category" (has its OWN content page *and*
      children, e.g. "Getting Started") — emitted as an article node that also
      parents its children.
    - without a ``slug`` → a pure structural folder — emitted as a url-less
      header.

Article URLs are ``<base>/<slug>`` where ``<base>`` is the source root
(``…/docs``). Hidden nodes (``isHidden``) are skipped.
"""

import json
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from app.services.profiles import registry
from app.services.profiles.base import TocEntry

# Angular serialises TransferState with a small set of short HTML entities so the
# JSON survives inside an HTML <script>. Reverse them before json.loads.
_ANGULAR_ENTITIES = {"&a;": "&", "&q;": '"', "&s;": "'", "&l;": "<", "&g;": ">"}

# The reader app's sidebar endpoint, relative to the site API path.
_TREE_PATH = "document/get-category-tree?x-versiontype=Knowledgebase"

# State subtrees that look like nav (children carrying categoryType) but are only
# the current article's sibling index — never the site's navigation.
_NOT_NAV_KEYS = {"articleIndex"}

_STATE_RE = re.compile(
    r'<script id="serverApp-state"[^>]*>(.*?)</script>', re.S | re.I
)

# Document360 categoryType for an "index" category: a landing node that only
# lists its children and has no article body of its own (confirmed against the
# real site — every categoryType==2 page returns no #articleContent, while
# categoryType==1 "page categories" DO carry a body). We emit these as url-less
# structural headers so they don't inflate the article total or waste a fetch;
# their child articles are still emitted and scraped.
_CATEGORY_TYPE_INDEX = 2


def _extract_state(html: str) -> dict | None:
    """Parse the Angular TransferState JSON out of the page, or None."""
    m = _STATE_RE.search(html)
    if not m:
        return None
    txt = m.group(1)
    for ent, ch in _ANGULAR_ENTITIES.items():
        txt = txt.replace(ent, ch)
    try:
        state = json.loads(txt)
    except Exception:
        return None
    return state if isinstance(state, dict) else None


def _node_count(node: dict) -> int:
    return 1 + sum(_node_count(c) for c in (node.get("children") or []))


def _find_nav_root(state: dict) -> dict | None:
    """Find the dict whose ``children`` is the navigation tree — the one with the
    most descendant nodes carrying ``categoryType`` (robust to key renames)."""
    best: dict | None = None
    best_n = -1

    def walk(o) -> None:
        nonlocal best, best_n
        if isinstance(o, dict):
            ch = o.get("children")
            if (
                isinstance(ch, list)
                and ch
                and any(isinstance(x, dict) and "categoryType" in x for x in ch)
            ):
                n = sum(_node_count(c) for c in ch if isinstance(c, dict))
                if n > best_n:
                    best, best_n = o, n
            for k, v in o.items():
                if k not in _NOT_NAV_KEYS:
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(state)
    return best


def parse_document360_nav(html: str, root_url: str) -> list[TocEntry]:
    """Build the ordered TOC from a Document360 page's embedded TransferState.

    Pure function (no I/O) so it can be unit-tested against a captured page."""
    state = _extract_state(html)
    if not state:
        return []
    return parse_document360_tree(state, root_url)


def parse_document360_tree(data, root_url: str) -> list[TocEntry]:
    """Build the ordered TOC from any JSON holding the nav tree — the
    get-category-tree response or a page's TransferState."""
    nav = _find_nav_root(data) if isinstance(data, (dict, list)) else None
    if not nav:
        return []

    p = urlparse(root_url)
    base = f"{p.scheme}://{p.netloc}{p.path.rstrip('/')}"

    out: list[TocEntry] = []
    seen: set[str] = set()

    def walk(nodes: list, level: int, parent_url: str | None) -> None:
        for n in sorted(
            (x for x in nodes if isinstance(x, dict)),
            key=lambda x: (x.get("order") or 0, x.get("title") or ""),
        ):
            if n.get("isHidden"):
                continue
            title = (n.get("title") or "").strip()
            slug = (n.get("slug") or "").strip()
            kids = n.get("children") or []
            is_index = n.get("categoryType") == _CATEGORY_TYPE_INDEX
            node_url: str | None = None
            if slug and not is_index:
                # A real article (articleType 0) or a "page category" (has its own
                # body AND children) — a scrapable page.
                node_url = f"{base}/{slug}"
                if node_url not in seen and title:
                    seen.add(node_url)
                    out.append(TocEntry(
                        title=title, url=node_url, level=level,
                        is_article=True, parent_url=parent_url,
                    ))
            elif title:
                # A pure folder (no slug) or a body-less index category
                # (categoryType==2) — a url-less structural header.
                out.append(TocEntry(
                    title=title, url=None, level=level,
                    is_article=False, parent_url=parent_url,
                ))
            if kids:
                walk(kids, level + 1, node_url or parent_url)

    walk(nav.get("children") or [], 0, None)
    return out


def _tree_api_url(root_url: str, state: dict | None) -> str:
    """The get-category-tree URL. The reader app rewrites ``/api`` to the
    project's ``siteApiPath`` on a custom domain (default ``api``)."""
    api = "api"
    if state:
        for key in ("project-primary-data-key", "project-info-key"):
            project = (state.get(key) or {}).get("project") or {}
            if project.get("siteApiPath"):
                api = str(project["siteApiPath"]).strip("/")
                break
    p = urlparse(root_url)
    return f"{p.scheme}://{p.netloc}/{api}/{_TREE_PATH}"


class Document360Profile:
    name = "document360"
    # Article bodies are server-rendered under #articleContent; the raw-HTTP path
    # fetches each page (with the realm's auth cookies) and extract_content_html
    # scopes to that container.
    content_engine = "raw_http"

    # Backoff/politeness for large KBs (Securiti is ~2,600 pages). Document360
    # (Cloudflare-fronted) rate-limits a fast parallel sweep with sporadic
    # 429/403s. Fetch a bit more gently and retry 403 too — 429/5xx are already
    # retried with exponential backoff by fetch_raw; 403 here is transient WAF
    # throttling under a valid session (a genuinely dead session is caught by the
    # raw_http consecutive-failure auth-expiry check, not by a lone 403).
    raw_http_concurrency = 4
    raw_http_retry_statuses = (403,)

    _EXCLUDE = [
        "script", "style",
        ".related-articles-container", ".article-feedback",
        ".feedback-buttons", "d360-article-rating",
    ]

    def detect(self, root_html: str, root_url: str) -> bool:
        low = root_html.lower()
        # "document360" appears in the site banner and the CDN script host;
        # "d360-article" is the rendered article web-component. Either is a
        # reliable, Document360-specific marker.
        return "document360" in low or "d360-article" in low

    async def build_toc(self, root_url: str, scraper) -> list[TocEntry]:
        try:
            html = await scraper.get_raw(root_url)
        except Exception:
            return []
        try:
            tree = json.loads(await scraper.get_raw(
                _tree_api_url(root_url, _extract_state(html))))
        except Exception:
            tree = None
        toc = parse_document360_tree(tree, root_url) if tree else []
        # Older reader builds have no tree endpoint but embed the nav in the page.
        return toc or parse_document360_nav(html, root_url)

    def extract_content_html(self, raw: str, url: str) -> str | None:
        """Scope a fetched article page to its rendered body (#articleContent),
        strip in-article chrome, and absolutise image URLs."""
        soup = BeautifulSoup(raw, "html.parser")
        el = soup.select_one("#articleContent") or soup.select_one(
            ".editor360-published-content"
        )
        if el is None:
            return None
        for sel in self._EXCLUDE:
            for junk in el.select(sel):
                junk.decompose()
        for img in el.find_all("img"):
            src = img.get("src")
            if src:
                img["src"] = urljoin(url, src)
        return str(el) if el.get_text(strip=True) else None

    def content_config(self) -> dict:
        # Fallback for any non-raw path; the raw path uses extract_content_html.
        return {
            "includeTags": ["#articleContent"],
            "excludeTags": [
                ".related-articles-container", ".article-feedback",
                ".feedback-buttons", "d360-article-rating",
            ],
            "onlyMainContent": True,
            "waitFor": 1500,
        }


PROFILE = Document360Profile()
registry.register(PROFILE)
