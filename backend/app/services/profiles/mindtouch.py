"""MindTouch (NICE CXone Expert) profile.

MindTouch sites (e.g. ConnectWise's ``docs.connectwise.com``, where Axcient's
x360 docs moved in 2026) are server-rendered; a source is one section of the
site (``https://docs.connectwise.com/x360Recover``). We build the ordered TOC
from the deki API's page tree — one call for the whole section, in the vendor's
custom order (it matches the site's own rendered sitemap page for page) — and
fetch each article over the raw-HTTP path, scoping to ``.mt-content-container``.

Access is behind a *guest* session, not a login: a cookieless page request is
bounced through the site's guest-auth bridge (on ConnectWise,
``university.connectwise.com/…/docs4authentication.ashx`` → ``@api/deki/users/
authenticate?authtoken=…``), which sets the session cookies and redirects back.
``fetch_raw``'s logged-out path follows such a chain carrying the cookies it
sets, so page and image fetches need nothing extra. The API is different: a
cookieless call is a flat 403 (no redirect to follow), and the bridge refuses to
mint a session for an API URL, so the tree is fetched with ``get_raw_in_session``
— a page visit first, then the API call inside that session. The page's own
``apiToken`` doesn't help; it is bound to the session cookie.

Every page with children is a MindTouch guide or category, and those are kept as
articles, not url-less headings: 20 of x360Recover's 63 carry substantial
content of their own (up to 33 KB, with screenshots). What MindTouch *generates*
into a page is dropped: ``.noindex`` blocks (the child-page card listings) and
the footer.

Revision dates are deliberately not captured. The API's ``date.modified`` is
per-page in form only: 6 of 8 sampled x360Recover pages carry the identical
``2026-03-23T22:08:52Z`` — the migration import — and the page's "Last updated"
field is filled in by JavaScript.
"""

import json
from urllib.parse import quote, urljoin, urlparse

from bs4 import BeautifulSoup

from app.services.profiles import registry
from app.services.profiles.base import TocEntry


def tree_api_url(root_url: str) -> str:
    """The deki page-tree URL for the section at *root_url*. The page path is
    double-encoded, as the deki API expects for ``=path`` page references."""
    p = urlparse(root_url)
    path = quote(quote(p.path.strip("/"), safe=""), safe="")
    return f"{p.scheme}://{p.netloc}/@api/deki/pages/={path}/tree?dream.out.format=json"


def _children(node: dict) -> list[dict]:
    # XML-to-JSON quirks: several children are a list, one is a bare dict, and
    # none is an empty string.
    sub = node.get("subpages")
    kids = sub.get("page") if isinstance(sub, dict) else None
    if isinstance(kids, dict):
        return [kids]
    return [k for k in kids or [] if isinstance(k, dict)]


def parse_mindtouch_tree(data) -> list[TocEntry]:
    """Flatten the API's tree (section root first, then its subtree depth-first,
    in API order) into TOC entries. The root sits beside its top-level children."""
    root = data.get("page") if isinstance(data, dict) else None
    if not isinstance(root, dict):
        return []
    out: list[TocEntry] = []

    def add(node: dict, level: int, parent_url: str | None) -> str | None:
        if node.get("@deleted") == "true":
            return None
        url = (node.get("uri.ui") or "").strip()
        title = (node.get("title") or "").strip()
        if not url or not title:
            return None
        out.append(TocEntry(title=title, url=url, level=level,
                            is_article=True, parent_url=parent_url))
        return url

    def walk(nodes: list[dict], level: int, parent_url: str | None) -> None:
        for n in nodes:
            url = add(n, level, parent_url)
            if url:
                walk(_children(n), level + 1, url)

    if add(root, 0, None):
        walk(_children(root), 0, None)
    return out


class MindTouchProfile:
    name = "mindtouch"
    content_engine = "raw_http"
    # One guest session for the whole run: the bridge refuses to mint one for a
    # page whose URL carries an escaped character (%3F, %2C, %E2%80%93, …) — 18 of
    # x360Recover's 556 — but serves that page to a session minted elsewhere.
    raw_http_shared_session = True

    # MindTouch's generated blocks inside the content container.
    _EXCLUDE = ["script", "style", ".noindex", "footer.mt-content-footer"]

    def detect(self, root_html: str, root_url: str) -> bool:
        low = root_html.lower()
        return 'content="mindtouch"' in low or "mt-content-container" in low

    async def build_toc(self, root_url: str, scraper) -> list[TocEntry]:
        api = tree_api_url(root_url)
        try:
            data = json.loads(await scraper.get_raw_in_session(root_url, api))
        except Exception:
            return []
        return parse_mindtouch_tree(data)

    def extract_content_html(self, raw: str, url: str) -> str | None:
        soup = BeautifulSoup(raw, "html.parser")
        el = soup.select_one(".mt-content-container")
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
            "includeTags": [".mt-content-container"],
            "excludeTags": [".noindex", ".mt-content-footer"],
            "onlyMainContent": True,
            "waitFor": 1500,
        }


registry.register(MindTouchProfile())
