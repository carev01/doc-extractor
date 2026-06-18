"""LLM-assisted extraction profile — gated, opt-in fallback for unrecognized sites.

This profile is **never auto-selected by detection** (``detect`` always returns
False).  It is inserted into the resolver's fallback chain ONLY when
``settings.llm_fallback_enabled`` is True, sitting between auto-detection and
the generic sitemap profile.

Design
------
The profile accepts an injectable *client* callable ``(html: str, root_url: str)
-> dict`` that returns a selector spec.  This keeps the LLM layer fully
unit-testable — tests pass a fake client and the real Anthropic HTTP path is
never exercised.  The default client is built lazily (only when the flag is on
and an API key is present) and calls the Anthropic Messages API via httpx.

The returned spec looks like::

    {
        "strategy": "sidebar" | "hubspoke" | "sitemap",
        # sidebar:
        "nav_selector": "...",
        # hubspoke:
        "category_link_selector": "...",
        "article_link_selector": "...",
        # optional for future use:
        "content_selector": "...",
    }

Documented follow-ons (NOT implemented here)
--------------------------------------------
- Per-source ``profile_config`` caching of the LLM-derived selector spec: the
  current ``ExtractionProfile`` interface does not expose the ``source`` object
  inside ``build_toc``, so the spec cannot be persisted back to the DB in this
  call.  Implement by adding an optional ``source`` parameter (or a
  ``set_source()`` hook) to the interface and storing ``source.profile_config``
  here.
- LLM-derived ``content_selector``: once per-source caching is in place, the
  spec's ``content_selector`` can be forwarded via ``content_config()`` so
  Firecrawl uses a precise CSS selector rather than the generic
  ``onlyMainContent`` heuristic.
"""

import json
import logging

import httpx

from app.core.config import settings
from app.services.profiles import registry
from app.services.profiles.base import TocEntry
from app.services.profiles.strategies import (
    hubspoke_toc,
    sidebar_tree_toc,
    sitemap_urls,
)

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are a documentation-site analyser.  Given the raw HTML of a documentation
site's root page and its URL, identify the best TOC extraction strategy and
return ONLY a JSON object (no markdown fences, no explanation) with these keys:

  strategy          — one of: "sidebar", "hubspoke", "sitemap"
  nav_selector      — CSS selector for the sidebar nav container (sidebar only)
  category_link_selector  — CSS selector for category links (hubspoke only)
  article_link_selector   — CSS selector for article links (hubspoke only)
  content_selector  — CSS selector for the main content area (all strategies,
                      optional; omit if uncertain)

Choose "sidebar" when the page has a persistent left-nav tree.
Choose "hubspoke" when the root lists discrete help-center categories.
Choose "sitemap" when neither pattern is identifiable from the HTML.
"""


def _build_default_client():
    """Return a synchronous callable that calls the Anthropic Messages API.

    This is the real implementation; it is only ever called when the flag is on
    and an API key is present.  Tests always inject a fake client instead.
    """
    import httpx as _httpx

    api_key = settings.anthropic_api_key

    def _call(html: str, root_url: str) -> dict:
        # Truncate to avoid hitting the context window; 20 000 chars is plenty
        # to detect nav patterns.
        snippet = html[:20_000]
        payload = {
            "model": "claude-haiku-4-5",
            "max_tokens": 512,
            "system": _SYSTEM_PROMPT,
            "messages": [
                {
                    "role": "user",
                    "content": (
                        f"URL: {root_url}\n\nHTML (truncated):\n{snippet}"
                    ),
                }
            ],
        }
        resp = _httpx.post(
            "https://api.anthropic.com/v1/messages",
            json=payload,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            timeout=30.0,
        )
        resp.raise_for_status()
        text = resp.json()["content"][0]["text"]
        return json.loads(text)

    return _call


class LlmProfile:
    """LLM-assisted profile for unrecognized documentation sites.

    Parameters
    ----------
    client:
        Optional injectable callable ``(html, root_url) -> dict``.  When
        *None*, a default client that calls the Anthropic Messages API is
        built lazily on first use.  Inject a fake in tests.
    """

    name = "llm"

    def __init__(self, client=None):
        self._client_override = client
        self._client_instance = None  # lazily initialised

    @property
    def _client(self):
        if self._client_override is not None:
            return self._client_override
        if self._client_instance is None:
            self._client_instance = _build_default_client()
        return self._client_instance

    def detect(self, root_html: str, root_url: str) -> bool:
        """Always returns False — LLM profile is never auto-selected."""
        return False

    async def build_toc(self, root_url: str, scraper) -> list[TocEntry]:
        """Ask the LLM which strategy to use, then dispatch to it.

        Returns ``[]`` when:
        - ``settings.llm_fallback_enabled`` is False (silent no-op gate).
        - The LLM call or strategy dispatch raises any exception (resilience).
        """
        if not settings.llm_fallback_enabled:
            return []

        try:
            html = await scraper.get_html(root_url)
            spec = self._client(html, root_url)
            strategy = spec.get("strategy", "sitemap")

            if strategy == "sidebar":
                return await sidebar_tree_toc(
                    scraper, root_url, spec["nav_selector"]
                )

            if strategy == "hubspoke":
                return await hubspoke_toc(
                    scraper,
                    root_url,
                    category_link_selector=spec["category_link_selector"],
                    article_link_selector=spec["article_link_selector"],
                )

            # "sitemap" or any unknown value — best-effort flat list
            urls = await sitemap_urls(scraper, root_url)
            return [
                TocEntry(title=u.rstrip("/").rsplit("/", 1)[-1] or u, url=u, level=0)
                for u in urls
            ]

        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "LLM fallback profile failed for %s: %s", root_url, exc
            )
            return []

    def content_config(self) -> dict:
        """Generic content-extraction config.

        ``onlyMainContent=True`` with a modest ``waitFor`` is a reasonable
        default for unrecognized sites.

        Follow-on: once per-source ``profile_config`` caching is available,
        use the LLM-derived ``content_selector`` here instead (see module
        docstring).
        """
        return {"onlyMainContent": True, "waitFor": 1500}


PROFILE = LlmProfile()
registry.register(PROFILE)
