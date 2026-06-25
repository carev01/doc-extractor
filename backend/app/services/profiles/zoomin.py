"""Zoomin (zDocs) documentation-portal profile.

Zoomin-powered portals are single-page apps: the page HTML is an empty shell and
both the nav and the article body are loaded from a JSON backend API hosted on a
sibling ``-be`` host. Neither a static GET nor a JS render of the page URL yields
content, so this profile talks to the API directly:

  TOC      GET  https://<backend>/api/bundle/<bundle>/toc?language=<lang>
                → nested ``[{title, nav_path, url, childEntries:[…]}]``
  CONTENT  GET  https://<backend>/api/bundle/<bundle>/page/<nav_path>?language=<lang>
                → ``{… "topic_html": "<article markup>" …}``

The backend host (e.g. ``help-be.example.com``) is read from the SPA shell's
embedded ``"host":"…"`` config; the bundle id comes from the ``/bundle/<id>/``
path; the language comes from the bundle metadata. Each TOC entry keeps the
human-facing page URL for display while pointing ``content_url`` at the API
endpoint, so the generic ``raw_http`` content path fetches the JSON and this
profile's ``extract_content_html`` unwraps ``topic_html``.
"""

import json
import re
from urllib.parse import urljoin, urlparse

from app.services.profiles import registry
from app.services.profiles.base import TocEntry

_BUNDLE_RE = re.compile(r"/bundle/([^/]+)")
_HOST_RE = re.compile(r'"host"\s*:\s*"([^"]+)"')


def _backend_host(shell_html: str, public_host: str) -> str:
    """Resolve the API backend host from the SPA shell, falling back to the
    Zoomin ``<label>-be.<domain>`` convention when it isn't embedded."""
    m = _HOST_RE.search(shell_html or "")
    if m and m.group(1):
        return m.group(1)
    label, _, rest = public_host.partition(".")
    return f"{label}-be.{rest}" if rest else public_host


class ZoominProfile:
    name = "zoomin"
    # Content comes from the JSON API (extract_content_html unwraps topic_html),
    # fetched per-entry via the entry's content_url — see module docstring.
    content_engine = "raw_http"

    def detect(self, root_html: str, root_url: str) -> bool:
        return "zDocsWebClient" in root_html or "zoominsoftware" in root_html

    async def build_toc(self, root_url: str, scraper) -> list[TocEntry]:
        m = _BUNDLE_RE.search(urlparse(root_url).path)
        if not m:
            return []
        bundle = m.group(1)
        public_host = urlparse(root_url).netloc

        shell = ""
        try:
            shell = await scraper.get_raw(root_url)
        except Exception:
            pass
        backend = _backend_host(shell, public_host)
        api_base = f"https://{backend}"

        language = await self._language(scraper, api_base, bundle)

        try:
            raw = await scraper.get_raw(
                f"{api_base}/api/bundle/{bundle}/toc?language={language}"
            )
            nodes = json.loads(raw)
        except Exception:
            return []
        if not isinstance(nodes, list):
            return []

        out: list[TocEntry] = []

        def page_url(host: str, nav_path: str) -> str:
            return f"https://{host}/bundle/{bundle}/page/{nav_path}"

        def walk(items, level: int, parent_url: str | None) -> None:
            for node in items:
                if not isinstance(node, dict):
                    continue
                title = (node.get("title") or "").strip()
                nav_path = node.get("nav_path") or ""
                kids = node.get("childEntries") or []
                url = content_url = None
                if nav_path:
                    url = page_url(public_host, nav_path)
                    content_url = (
                        f"{api_base}/api/bundle/{bundle}/page/{nav_path}"
                        f"?language={language}"
                    )
                if not title and not kids:
                    continue
                out.append(TocEntry(
                    title=title or url or "", url=url, level=level,
                    is_article=bool(url) and not kids, parent_url=parent_url,
                    content_url=content_url,
                ))
                if kids:
                    walk(kids, level + 1, url)

        walk(nodes, 0, None)
        return out

    async def _language(self, scraper, api_base: str, bundle: str) -> str:
        """First available bundle language (Zoomin uses ``enus``, not ``en-us``)."""
        try:
            meta = json.loads(await scraper.get_raw(f"{api_base}/api/bundle/{bundle}"))
            langs = (meta.get("bundle") or {}).get("available_languages") or []
            if langs:
                return langs[0]
        except Exception:
            pass
        return "enus"

    def extract_content_html(self, raw: str, url: str) -> str | None:
        """Unwrap ``topic_html`` from the page API JSON and absolutise images."""
        try:
            data = json.loads(raw)
        except Exception:
            return None
        html = data.get("topic_html") if isinstance(data, dict) else None
        if not html:
            return None
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        for img in soup.find_all("img"):
            src = img.get("src")
            if src:
                img["src"] = urljoin(url, src)
        return str(soup)


PROFILE = ZoominProfile()
registry.register(PROFILE)
