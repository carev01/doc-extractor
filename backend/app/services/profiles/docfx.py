"""DocFX / Open-Publishing documentation profile.

The DocFX toolchain (and the Microsoft Open-Publishing platform built on its
lineage) renders the left nav client-side from a sibling ``toc.json``, so the
tree isn't in the page HTML. The JSON is a nested list of
``{toc_title|name, href, children:[…]}`` nodes; ``href`` may be a bare slug
(extensionless page in the same directory), a ``../`` cross-section reference,
or an absolute off-site link, and often carries breadcrumb ``?toc=…&bc=…`` query
hints. We fetch and resolve it directly, dropping off-site links and the query
hints to get clean canonical page URLs.

Content: the article body is served as static HTML under ``.content``, so the
content path is a plain GET (``raw_http``).
"""

from urllib.parse import urljoin, urlparse

from app.services.profiles import registry
from app.services.profiles.strategies import json_toc
from app.services.profiles.base import TocEntry

_TOC_FILENAME = "toc.json"


class DocFxProfile:
    name = "docfx"
    # Article bodies render as static HTML under .content (see content_config).
    content_engine = "raw_http"

    def detect(self, root_html: str, root_url: str) -> bool:
        lower = root_html.lower()
        # DocFX emits a generator meta tag.
        if 'content="docfx' in lower:
            return True
        # The Open-Publishing platform (learn.microsoft.com) emits ms.* <meta>
        # tags AND data-bi-name telemetry attributes. The head <meta ms.*> tags
        # don't survive a JS render (Firecrawl returns the rendered DOM, where
        # they're dropped — which silently broke Azure Backup detection), but
        # data-bi-name does. Key on data-bi-name, paired with an ms.* / learn
        # marker so it can't false-positive on an unrelated site.
        return "data-bi-name" in lower and (
            'name="ms.' in lower or "learn.microsoft" in lower
        )

    async def build_toc(self, root_url: str, scraper) -> list[TocEntry]:
        toc_url = urljoin(root_url, _TOC_FILENAME)
        host = urlparse(root_url).netloc
        return await json_toc(
            scraper, toc_url,
            items_key="items", children_key="children",
            title_keys=("toc_title", "name", "title"), href_key="href",
            host_allow={host} if host else None,
            strip_query=True,
        )

    def content_config(self) -> dict:
        return {
            "includeTags": [".content"],
            # The metadata bar / feedback / "next steps" chrome the platform
            # renders alongside the article body.
            "excludeTags": [".page-metadata", ".feedback-verbatim", ".ms-feedback"],
            "onlyMainContent": False,
            "waitFor": 1500,
        }


PROFILE = DocFxProfile()
registry.register(PROFILE)
