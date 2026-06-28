"""Rspress static-docs profile (full nav tree + bodies pre-rendered into HTML).

Targets documentation sites built on the Rspress framework (rspress.dev), whose
entire sidebar nav AND article bodies are server-rendered into every page's
static HTML. After JS hydration Rspress collapses the sidebar to the current
page, so a rendered scrape sees only one nav link; the full tree exists only in
the raw HTML. Both the tree and the bodies are static, so the profile runs
entirely on the raw_http path (plain GET + local scoping, no render).

Validated against AvePoint Learn (learn.avepoint.com); keyed on the generic
Rspress fingerprint, not the vendor.
"""

from app.services.profiles import registry
from app.services.profiles.base import TocEntry


class RspressProfile:
    name = "rspress"
    # Whole tree + bodies are static HTML; fetch directly, no render.
    content_engine = "raw_http"

    def detect(self, root_html: str, root_url: str) -> bool:
        # The sidebar nav hook plus the article hook together are distinctive to
        # Rspress; either alone is too generic.
        return "rspress-doc" in root_html and "rspress-sidebar" in root_html

    async def build_toc(self, root_url: str, scraper) -> list[TocEntry]:
        return []  # implemented in Task 2

    def content_config(self) -> dict:
        return {
            "includeTags": [".rspress-doc"],
            # Drop the right-rail "on this page" TOC, the prev/next + edit
            # footer, and any in-doc breadcrumb/nav.
            "excludeTags": [".rspress-local-toc-container", ".rspress-doc-footer", "nav"],
            "onlyMainContent": False,
        }


PROFILE = RspressProfile()
# Registered in profiles/__init__.py in Task 2 (once build_toc is implemented).
