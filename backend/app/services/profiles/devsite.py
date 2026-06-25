"""Devsite documentation profile (Google's open documentation framework).

Devsite (used across Google's developer/cloud documentation) renders the left
nav as a persistent ``<nav class="devsite-book-nav">`` whose book tree is a
``<ul class="devsite-nav-list" menu="_book">``. The generic ``nav.devsite-nav``
selector matches the *outer* nav, whose first ``<ul>`` is the small product-tab
list (Overview / Guides / Reference) — so a naive sidebar parse captures only
those tabs. Target the book ``<ul>`` directly to get the full article tree.

Content: ``.devsite-article-body`` — the article wrapper — is served as static
HTML, so the body is fetched with a plain GET (``content_engine = "raw_http"``)
while the TOC is read from a single render of the (also static) book nav.
"""

from app.services.profiles import registry
from app.services.profiles.strategies import sidebar_tree_toc
from app.services.profiles.base import TocEntry

# The book tree, not the outer product-tab nav.
_BOOK_NAV_SELECTOR = 'ul.devsite-nav-list[menu="_book"]'


class DevsiteProfile:
    name = "devsite"
    # Article bodies are static under .devsite-article-body (see content_config),
    # so fetch them directly — no JS render needed for content.
    content_engine = "raw_http"

    def detect(self, root_html: str, root_url: str) -> bool:
        return "devsite-book-nav" in root_html or "devsite-nav-list" in root_html

    async def build_toc(self, root_url: str, scraper) -> list[TocEntry]:
        return await sidebar_tree_toc(scraper, root_url, _BOOK_NAV_SELECTOR)

    def content_config(self) -> dict:
        return {
            "includeTags": [".devsite-article-body"],
            # Devsite injects feedback widgets and "Send feedback" links inside
            # the article wrapper; drop them so they don't leak into the body.
            "excludeTags": [".devsite-article-meta", ".devsite-floating-action-buttons"],
            "onlyMainContent": False,
            "waitFor": 1500,
        }


PROFILE = DevsiteProfile()
registry.register(PROFILE)
