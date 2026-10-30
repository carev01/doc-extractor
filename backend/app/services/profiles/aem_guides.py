"""Adobe Experience Manager Guides portal (DITA published through AEM Sites).

Targets Arctera's documentation portal (``docs.backupexec.com`` — Backup Exec,
System Recovery), whose guide pages are rendered by AEM Guides' topic renderer
inside AEM Core Components. The name is the platform, not the vendor.

Every page carries the guide's **complete** nested nav, server-rendered under
``nav.cmp-navigation`` (``li.cmp-navigation__item--level-N`` with the link in an
``__item-container`` div and children in a sibling ``ul.cmp-navigation__group``),
and the topic body is static too — so the profile runs entirely on raw_http:

* TOC: GET the source's page once and parse that nav. Section parents link to
  real landing pages, so they keep their URL and are scraped. Order is DOM order.
  The Administrator's Guide is 973 entries, five levels deep.
* Content: the ``<h1>`` from ``header.topic-renderer__header`` plus
  ``.topic-renderer__content``. The rest of the header (Table of Contents / PDF
  Export / Share toolbar), the breadcrumb and the "Last published" line are
  chrome. That date is a site publish stamp, not a page revision — pages of a
  guide carry it within milliseconds of each other — so it is deliberately not
  captured (see page_dates: a fabricated date is worse than an honest NULL).

Two portal traits shape the rest:

* **Rate limiting.** The portal answered 429 after four requests in ~5s, while
  one request every ~3s never tripped it. Pages are fetched one at a time with a
  delay; fetch_raw already retries 429 with backoff.
* **The version is spelled into slugs** (``backup-exec-251-readme/…``,
  ``…-wizard-help-v251/…``, ``…-24-1-readme/…``), so ``slug_versions`` opts these
  sources into templating it there too (see versioning.derive_topic_key).
"""

from urllib.parse import urljoin

from bs4 import BeautifulSoup

from app.services.profiles import registry
from app.services.profiles.base import TocEntry
from app.services.profiles.strategies import parse_sidebar_tree

_NAV_SELECTOR = "nav.cmp-navigation"
_TITLE_SELECTOR = "main.topic-renderer header.topic-renderer__header h1"
_BODY_SELECTOR = "main.topic-renderer .topic-renderer__content"


class AemGuidesProfile:
    name = "aem_guides"
    # Whole nav + bodies are static HTML; fetch directly, no render.
    content_engine = "raw_http"
    raw_http_concurrency = 1
    raw_http_request_delay = 2.0
    slug_versions = True

    def detect(self, root_html: str, root_url: str) -> bool:
        # The topic renderer is AEM Guides'; cmp-navigation alone is any AEM site.
        return "topic-renderer__content" in root_html and "cmp-navigation" in root_html

    async def build_toc(self, root_url: str, scraper) -> list[TocEntry]:
        try:
            html = await scraper.get_raw(root_url)
        except Exception:
            return []
        return parse_sidebar_tree(html or "", root_url, _NAV_SELECTOR)

    def extract_content_html(self, raw: str, url: str) -> str | None:
        """Scope a raw page to its title and topic body, with absolute image URLs.

        None when there is no topic body, which the raw_http failure-rate guard
        reports as a rate (grep "No content body found at" for the URLs).
        """
        soup = BeautifulSoup(raw, "html.parser")
        body = soup.select_one(_BODY_SELECTOR)
        if body is None:
            return None
        for img in body.find_all("img"):
            src = img.get("src")
            if src:
                img["src"] = urljoin(url, src)
        title = soup.select_one(_TITLE_SELECTOR)
        return (str(title) if title else "") + str(body)

    def content_config(self) -> dict:
        # Unused on the raw_http path (extract_content_html does the scoping), but
        # extract_source always calls content_config(), so it must exist.
        return {
            "includeTags": [_TITLE_SELECTOR, _BODY_SELECTOR],
            "onlyMainContent": False,
            "waitFor": 1500,
        }


PROFILE = AemGuidesProfile()
registry.register(PROFILE)
