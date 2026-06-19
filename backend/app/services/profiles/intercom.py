"""Intercom Help Center profile (hub-and-spoke layout).

Intercom-powered help centers (e.g. help.druva.com) use a root "home" page
that lists *collections* (categories), each linking to a set of *articles*.

Layout:
  root → a.collection-link (collections/categories, level 0)
       → a[data-testid="article-link"] (articles, level 1)

Title caveat:
  The collection anchor's full text concatenates title + description + article
  count (e.g. "Druva Cloud PlatformGetting started…249 articles").  The clean
  title lives in the sub-element ``[data-testid="collection-name"]``.
  For article links the description is in ``[data-testid="article-description"]``
  and the title is the first ``<span>`` without a ``data-testid`` attribute.
  We use ``hubspoke_toc``'s ``category_title_selector`` /
  ``article_title_selector`` params (Option A) to extract clean titles.

Detection fingerprint:
  ``a.collection-link`` is unique to Intercom-generated help centres and
  is absent from all other supported platform fixtures.
"""

from app.services.profiles import registry
from app.services.profiles.base import TocEntry
from app.services.profiles.strategies import hubspoke_toc



class IntercomProfile:
    name = "intercom"

    def detect(self, root_html: str, root_url: str) -> bool:
        """Return True when the page contains Intercom's collection-link anchor.

        ``a.collection-link`` appears on every Intercom help-center home page
        and is absent from all other supported platform fixtures.
        The string ``collection-link`` is a stable, Intercom-specific marker.
        """
        return "collection-link" in root_html

    async def build_toc(self, root_url: str, scraper) -> list[TocEntry]:
        return await hubspoke_toc(
            scraper,
            root_url,
            category_link_selector="a.collection-link",
            # Intercom uses data-testid="article-link" for both article links
            # AND sub-collection links; filter to /articles/ hrefs only.
            article_link_selector='a[data-testid="article-link"][href*="/articles/"]',
            category_title_selector='[data-testid="collection-name"]',
            # Article title: first <span> without a data-testid (title span).
            # The description span carries data-testid="article-description".
            article_title_selector="span:not([data-testid])",
        )

    def content_config(self) -> dict:
        return {
            # No includeTags (onlyMainContent heuristic), so drop Intercom Help
            # Center chrome explicitly: the article reaction/feedback widget and
            # the related-articles block. No-op when absent.
            "excludeTags": [
                ".intercom-interblocks-article-reactions",
                ".intercom-interblocks-related-articles",
            ],
            "onlyMainContent": True,
            "waitFor": 1500,
        }


PROFILE = IntercomProfile()
registry.register(PROFILE)
