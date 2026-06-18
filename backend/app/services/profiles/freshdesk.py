"""Freshdesk Help Center profile (3-level hub-and-spoke).

Freshdesk-powered help centers (e.g. help.keepit.com) use a root "home" page
that lists *solutions* (categories), each linking to *folders* (sections), each
containing *articles*.

Layout:
  root → a[href*="/support/solutions/<id>"]  (categories, level 0)
       → a[href*="/support/solutions/folders/<id>"]  (folders/sections, level 1)
       → a[href*="/support/solutions/articles/<id>"]  (articles, level 2)

The home page also contains a "Partners" folder link directly (skips category
level) and a "Popular topics" widget with article links.  The category selector
uses CSS :not() to exclude folder and article paths, keeping only the
``/support/solutions/<digits>`` category links.

Detection fingerprint:
  ``cdn.freshdesk.com`` appears in every Freshdesk-hosted help center page
  (favicon/CSS CDN URLs) and is absent from all other supported platform
  fixtures.
"""

from app.services.profiles import registry
from app.services.profiles.base import TocEntry
from app.services.profiles.strategies import hubspoke_toc

ROOT = "https://help.keepit.com/support/home"


class FreshdeskProfile:
    name = "freshdesk"

    def detect(self, root_html: str, root_url: str) -> bool:
        """Return True when the page contains the Freshdesk CDN hostname.

        ``cdn.freshdesk.com`` is present in Freshdesk-hosted help center pages
        (favicon and stylesheet links) and is absent from all other supported
        platform fixtures.
        """
        return "cdn.freshdesk.com" in root_html

    async def build_toc(self, root_url: str, scraper) -> list[TocEntry]:
        return await hubspoke_toc(
            scraper,
            root_url,
            # Category links: /support/solutions/<digits> only — exclude folder
            # and article sub-paths that also contain "/support/solutions/".
            category_link_selector=(
                'a[href*="/support/solutions/"]'
                ':not([href*="/solutions/folders/"])'
                ':not([href*="/solutions/articles/"])'
            ),
            section_link_selector='a[href*="/support/solutions/folders/"]',
            article_link_selector='a[href*="/support/solutions/articles/"]',
        )

    def content_config(self) -> dict:
        return {
            "onlyMainContent": True,
            "waitFor": 1500,
        }


PROFILE = FreshdeskProfile()
registry.register(PROFILE)
