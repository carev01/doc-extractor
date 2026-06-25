"""Single-page release-notes / changelog profile.

Some help centers publish all their release notes on one ``/help/product-updates/``
page, holding several independent feeds in separate sections — e.g. a Platform
feed in ``<div id="updates" class="releasenotes">`` and a Partner Management
Console feed in ``<div id="pmc" class="releasenotes">``. There are no per-release
URLs; every release is an inline ``<h3>`` inside its feed.

Each feed is extracted as its own article from the *same* page URL, using a
per-entry ``content_selector`` (``#updates`` / ``#pmc``) so the two feeds become
two distinct documents. Content is rendered via Browserless and sliced to the
feed section — no warm-up navigation is needed (the page isn't WAF-gated); the
warm-up render path is reused purely for its "render then take selector
innerHTML" behaviour.

Detection is path-based: the ``releasenotes`` class also appears in shared
chrome on unrelated Keepit pages, so we match the publisher host plus the
``product-updates`` path instead.
"""

from urllib.parse import urldefrag, urlparse

from bs4 import BeautifulSoup

from app.services.profiles import registry
from app.services.profiles.base import TocEntry

_PUBLISHER_HOST = "keepit.com"
_PRODUCT_UPDATES_PATH = "product-updates"
# Zero-width characters Keepit sprinkles into headings.
_ZERO_WIDTH = "​﻿"


def _clean_title(text: str) -> str:
    return text.translate({ord(c): None for c in _ZERO_WIDTH}).strip()


class ReleaseNotesProfile:
    name = "release_notes"
    render_engine = "browserless"

    def detect(self, root_html: str, root_url: str) -> bool:
        p = urlparse(root_url)
        return _PUBLISHER_HOST in p.netloc.lower() and _PRODUCT_UPDATES_PATH in p.path

    async def build_toc(self, root_url: str, scraper) -> list[TocEntry]:
        soup = BeautifulSoup(await scraper.get_html(root_url), "html.parser")
        base, _ = urldefrag(root_url)
        out: list[TocEntry] = []
        for feed in soup.select(".releasenotes[id]"):
            fid = feed.get("id")
            if not fid:
                continue
            heading = feed.find(["h2", "h3"])
            title = _clean_title(heading.get_text(strip=True)) if heading else fid
            out.append(TocEntry(
                title=title,
                url=f"{base}#{fid}",
                level=0,
                is_article=True,
                content_selector=f"#{fid}",
            ))
        return out

    def browserless_content_spec(self) -> dict:
        # No run-wide selector (per-entry content_selector drives extraction) and
        # no warm-up navigation (the page isn't WAF-gated).
        return {"selector": None, "warmup_url": None}

    def content_config(self) -> dict:
        # Unused for content (render_engine=browserless); kept for parity.
        return {"onlyMainContent": False, "waitFor": 2000}


PROFILE = ReleaseNotesProfile()
registry.register(PROFILE)
