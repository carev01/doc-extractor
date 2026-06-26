"""DITA documentation portal with a TOC/content API (e.g. IBM Documentation).

Targets documentation platforms that ship DITA-generated topics through a
versioned TOC/content REST API behind a JS single-page app — currently IBM
Documentation (``www.ibm.com/docs``), the same engine across every IBM product.
The name is the generic mechanism (DITA topics + JSON/HTML API), not the vendor.

The page HTML is an empty shell; both the navigation tree and the topic bodies
are loaded client-side from a JSON/HTML API (discovered in the SPA bundle):

    GET /docs/api/v1/toc/<product>/<version>?lang=<lang>
        -> {"toc": {"label", "href", "topicId", "topics": [ ...recursive ]}}
    GET /docs/api/v1/content/<href>?parsebody=true&lang=<lang>
        -> the topic body as static HTML (a DITA topic)

So the whole ordered tree is built from the toc API (no rendering), and each
topic's body is fetched from the content API — the raw_http path, with the
content URL carried per-entry in ``content_url`` (distinct from the human-facing
``?topic=`` display URL).

A source points at a product/version landing (``/docs/<lang>/<product>/<version>``,
e.g. ``/docs/en/spfd/8.2.1``); the whole product tree is extracted. The internal
content id (e.g. ``SSER7G_8.2.1``) comes from the toc ``href`` values; the public
``<product>/<version>`` shortname (``spfd/8.2.1``) keys the toc API and the
display URLs.

Topic bodies are DITA HTML scoped by ``<article>`` (== ``[role=main]``); the
``.familylinks`` parent-topic breadcrumb is dropped (see content_config).
"""

import json
import re
from urllib.parse import urlparse

from app.services.profiles import registry
from app.services.profiles.base import TocEntry

# /docs/<lang>/<product>/<version>[/...] — the public product-landing path.
_DOCS_PATH_RE = re.compile(r"/docs/([a-z]{2})/([^/]+)/([^/?#]+)")


class DitaApiProfile:
    name = "dita_api"
    # Topic bodies come from the content API as static HTML, fetched per-entry
    # via content_url and scoped by the generic raw_http scoper (content_config).
    content_engine = "raw_http"

    MAX_NODES = 20000  # safety bound on tree size

    def detect(self, root_html: str, root_url: str) -> bool:
        # The www.ibm.com/docs path is unique to the platform and reliable even
        # though the page itself is a JS shell; the "ibmdocs" marker in the shell
        # is a secondary confirmation.
        host = urlparse(root_url).netloc.lower()
        if host.endswith("ibm.com") and "/docs/" in urlparse(root_url).path:
            return True
        return "ibmdocs" in root_html.lower()

    async def build_toc(self, root_url: str, scraper) -> list[TocEntry]:
        m = _DOCS_PATH_RE.search(root_url)
        if not m:
            return []
        lang, product, version = m.group(1), m.group(2), m.group(3)
        p = urlparse(root_url)
        origin = f"{p.scheme}://{p.netloc}"

        toc_url = f"{origin}/docs/api/v1/toc/{product}/{version}?lang={lang}"
        try:
            data = json.loads(await scraper.get_raw(toc_url))
        except Exception:
            return []
        root = data.get("toc") if isinstance(data, dict) else None
        if not isinstance(root, dict):
            return []

        out: list[TocEntry] = []

        def content_url(href: str) -> str:
            return f"{origin}/docs/api/v1/content/{href}?parsebody=true&lang={lang}"

        def display_url(node: dict, href: str) -> str:
            topic_id = node.get("topicId")
            if topic_id:
                return f"{origin}/docs/{lang}/{product}/{version}?topic={topic_id}"
            return content_url(href)

        def walk(nodes: list, level: int) -> None:
            for node in nodes:
                if len(out) >= self.MAX_NODES:
                    return
                href = (node.get("href") or "").strip()
                title = (node.get("label") or "").strip()
                kids = node.get("topics") or []
                # Every toc node is a real topic page (it has an href even when
                # it also has children). Emit only nodes whose href is an actual
                # topic file; structural-only hrefs (the product root, which has
                # no .html) become url-less section headers.
                if href.endswith(".html"):
                    out.append(TocEntry(
                        title=title or href,
                        url=display_url(node, href),
                        level=level, is_article=True, parent_url=None,
                        content_url=content_url(href),
                    ))
                elif title:
                    out.append(TocEntry(
                        title=title, url=None, level=level,
                        is_article=False, parent_url=None,
                    ))
                if kids:
                    walk(kids, level + 1)

        # Skip the synthetic product-title root wrapper (its name is already the
        # DocExtractor product); emit its top-level topics at level 0.
        walk(root.get("topics") or [], 0)
        return out

    def content_config(self) -> dict:
        return {
            # DITA topic body. <article role=article> wraps the title + body;
            # drop the parent-topic breadcrumb (.familylinks/.parentlink).
            "includeTags": ["article"],
            "excludeTags": [".familylinks", ".parentlink", ".breadcrumb"],
            "onlyMainContent": False,
            "waitFor": 1500,
        }


PROFILE = DitaApiProfile()
registry.register(PROFILE)
