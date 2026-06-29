"""Oxygen XML WebHelp profile (e.g. Rubrik docs.rubrik.com).

Login-walled + WAF-protected. Content is server-rendered in-page (``article``);
the full TOC is not in any single page (the on-page #wh_publication_toc is
contextual) and there is no TOC file — but Oxygen's search index ships a
complete page inventory at
``<pub_root>/oxygen-webhelp/app/search/index/htmlFileInfoList.js``. build_toc
uses that for a complete (flat, URL-ordered) TOC; the authored hierarchy is
rebuilt post-scrape from per-page #wh_publication_toc fragments (see rebuild_toc,
added later). Runs on the authenticated raw-HTTP path, paced for the WAF.
"""

import json
import re
from urllib.parse import urljoin

from app.services.profiles import registry
from app.services.profiles.base import TocEntry

_INVENTORY_REL = "oxygen-webhelp/app/search/index/htmlFileInfoList.js"
_OXYGEN_REF = re.compile(r'["\'](?:[^"\']*?/)?oxygen-webhelp/')


def _pub_root(page_url: str, html: str) -> str | None:
    """The publication root: the absolute URL up to (and including) the segment
    before ``oxygen-webhelp/``, derived from an oxygen-webhelp asset ref."""
    m = _OXYGEN_REF.search(html)
    if not m:
        return None
    ref = m.group(0).strip('"\'')
    abs_ref = urljoin(page_url, ref)            # .../en-us/saas/oxygen-webhelp/
    return abs_ref.split("oxygen-webhelp/")[0]  # .../en-us/saas/


class OxygenWebhelpProfile:
    name = "oxygen_webhelp"
    content_engine = "raw_http"
    # WAF pacing (read by the raw-HTTP content path).
    raw_http_concurrency = 2
    raw_http_request_delay = 0.3
    raw_http_retry_statuses = (401, 429, 502, 503, 504)
    # Capture each page's TOC tree fragment for the post-process hierarchy rebuild.
    toc_fragment_selector = "#wh_publication_toc"

    def detect(self, root_html: str, root_url: str) -> bool:
        return "oxygen-webhelp" in root_html and "wh_publication_toc" in root_html

    async def build_toc(self, root_url: str, scraper) -> list[TocEntry]:
        try:
            html = await scraper.get_raw(root_url)
        except Exception:
            return []
        pub_root = _pub_root(root_url, html or "")
        if not pub_root:
            return []
        try:
            raw = await scraper.get_raw(pub_root + _INVENTORY_REL)
        except Exception:
            return []
        m = re.search(r"htmlFileInfoList\s*=\s*(\[.*\])", raw or "", re.S)
        if not m:
            return []
        try:
            entries = json.loads(m.group(1))
        except Exception:
            return []
        out: list[TocEntry] = []
        seen: set[str] = set()
        for s in entries:
            parts = s.split("@@@")
            path = parts[0].strip()
            if not path or path in seen:
                continue
            seen.add(path)
            title = parts[1].strip() if len(parts) > 1 and parts[1].strip() else path
            out.append(TocEntry(
                title=title, url=urljoin(pub_root, path),
                level=path.count("/"), is_article=True,
            ))
        return out

    def content_config(self) -> dict:
        return {
            "includeTags": ["article"],
            "excludeTags": [".related-links", "nav", "header", "footer", ".wh_breadcrumb"],
            "onlyMainContent": False,
        }


PROFILE = OxygenWebhelpProfile()
registry.register(PROFILE)
