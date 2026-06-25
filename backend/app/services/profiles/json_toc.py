"""Sibling-JSON-TOC documentation profile.

Some documentation sites build their left nav client-side from a JSON file that
sits next to the topic pages (``toc-contents.json``), so the nav never appears
in the page HTML — a single render captures nothing usable. This profile fetches
that JSON directly and resolves it into the ordered tree (mirroring the approach
``flare_helpsystem_toc`` uses for MadCap Flare's ``Data/`` files).

The JSON is a nested list of ``{title, href, contents:[…]}`` nodes; ``href`` is
relative to the guide directory. Article bodies are served as static HTML under
``#main-col-body``, so the content path is a plain GET (``raw_http``).
"""

from urllib.parse import urljoin, urlparse

from app.services.profiles import registry
from app.services.profiles.strategies import json_toc
from app.services.profiles.base import TocEntry

_TOC_FILENAME = "toc-contents.json"


class JsonTocProfile:
    name = "json_toc"
    # #main-col-body is server-rendered static HTML (see content_config) — fetch
    # the body directly; the JSON TOC is likewise fetched, never rendered.
    content_engine = "raw_http"

    def detect(self, root_html: str, root_url: str) -> bool:
        # The doc shell carries id="awsdocs-…" wrappers and references the
        # sibling toc-contents.json; both are absent from other platforms.
        return "awsdocs-" in root_html or _TOC_FILENAME in root_html

    async def build_toc(self, root_url: str, scraper) -> list[TocEntry]:
        toc_url = urljoin(root_url, _TOC_FILENAME)
        host = urlparse(root_url).netloc
        return await json_toc(
            scraper, toc_url,
            items_key="contents", children_key="contents",
            title_keys=("title",), href_key="href",
            host_allow={host} if host else None,
        )

    def content_config(self) -> dict:
        return {
            "includeTags": ["#main-col-body"],
            "excludeTags": ["#awsdocs-page-tools", ".awsdocs-page-tools", ".feedback"],
            "onlyMainContent": False,
            "waitFor": 1500,
        }


PROFILE = JsonTocProfile()
registry.register(PROFILE)
