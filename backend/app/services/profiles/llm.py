"""LLM-assisted extraction profile — gated, opt-in fallback for unrecognized sites.

This module provides two cooperating pieces:

1. ``derive_spec(html, root_url) -> dict | None``
   Async function that calls an LLM (Anthropic or OpenAI-compatible) to analyse
   a documentation root page and return a selector spec.  Multi-provider: branch
   on ``settings.llm_provider`` ("anthropic" | "openai").  Returns ``None`` on
   any error or missing API key.

2. ``DerivedProfile(spec)``
   A parameterized profile built from a cached/fresh spec.  Its ``build_toc``
   dispatches to the same strategy helpers as the original profile; its
   ``content_config`` is spec-aware: when the LLM identified a ``content_selector``
   it forwards that CSS selector to Firecrawl instead of the generic heuristic.

3. ``LlmProfile``
   Thin safety-net; registered as ``"llm"`` in the registry so the UI option and
   ``registry.get("llm")`` still resolve.  ``build_toc`` scrapes the root HTML,
   calls ``derive_spec``, and delegates to ``DerivedProfile``.

Spec caching (the "source hook")
---------------------------------
``_resolve_profile`` in ``firecrawl.py`` is the primary caching site.  When it
enters the LLM branch it reads ``source.profile_config["llm_spec"]`` (cache hit)
or calls ``derive_spec`` and writes the result back (cache miss).  The existing
``await db.commit()`` immediately after ``_resolve_profile`` persists the new
spec.  Subsequent extractions of the same source skip re-derivation entirely.

The spec shape::

    {
        "strategy": "sidebar" | "hubspoke" | "sitemap",
        # sidebar:
        "nav_selector": "...",
        # hubspoke:
        "category_link_selector": "...",
        "article_link_selector": "...",
        # optional:
        "content_selector": "...",
    }
"""

import json
import logging
import re

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

# Provider defaults
_ANTHROPIC_BASE_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_DEFAULT_MODEL = "claude-haiku-4-5"
_OPENAI_BASE_URL = "https://api.openai.com/v1/chat/completions"
_OPENAI_DEFAULT_MODEL = "gpt-4o-mini"

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _strip_fences(text: str) -> str:
    """Remove markdown code fences defensively."""
    m = _FENCE_RE.search(text)
    return m.group(1) if m else text.strip()


async def derive_spec(html: str, root_url: str) -> "dict | None":
    """Call the configured LLM to derive a selector spec for a documentation root page.

    Parameters
    ----------
    html:
        Raw HTML of the root page (will be truncated to 20 000 chars).
    root_url:
        Canonical URL of the root page (included in the prompt for context).

    Returns
    -------
    dict
        The parsed spec (keys: strategy, nav_selector, …) on success.
    None
        On missing API key, HTTP error, JSON parse failure, or any other
        exception (logged as a warning).
    """
    api_key = settings.llm_api_key
    if not api_key:
        logger.warning("LLM derive_spec skipped: llm_api_key is not set")
        return None

    provider = settings.llm_provider
    snippet = html[:20_000]

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            if provider == "anthropic":
                base_url = settings.llm_base_url or _ANTHROPIC_BASE_URL
                model = settings.llm_model or _ANTHROPIC_DEFAULT_MODEL
                headers = {
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                }
                body = {
                    "model": model,
                    "max_tokens": settings.llm_max_tokens,
                    "system": _SYSTEM_PROMPT,
                    "messages": [
                        {
                            "role": "user",
                            "content": f"URL: {root_url}\n\nHTML (truncated):\n{snippet}",
                        }
                    ],
                }
                resp = await client.post(base_url, headers=headers, json=body)
                resp.raise_for_status()
                text = resp.json()["content"][0]["text"]

            elif provider == "openai":
                base_url = settings.llm_base_url or _OPENAI_BASE_URL
                model = settings.llm_model or _OPENAI_DEFAULT_MODEL
                headers = {
                    "Authorization": f"Bearer {api_key}",
                    "content-type": "application/json",
                }
                body = {
                    "model": model,
                    "max_tokens": settings.llm_max_tokens,
                    "messages": [
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {
                            "role": "user",
                            "content": f"URL: {root_url}\n\nHTML (truncated):\n{snippet}",
                        },
                    ],
                    "response_format": {"type": "json_object"},
                }
                resp = await client.post(base_url, headers=headers, json=body)
                resp.raise_for_status()
                text = resp.json()["choices"][0]["message"]["content"]

            else:
                logger.warning("LLM derive_spec: unknown provider %r", provider)
                return None

        return json.loads(_strip_fences(text))

    except Exception as exc:  # noqa: BLE001
        logger.warning("LLM derive_spec failed for %s: %s", root_url, exc)
        return None


class DerivedProfile:
    """Parameterized extraction profile built from an LLM-derived spec.

    This is the runtime profile returned by ``_resolve_profile`` when the LLM
    branch is taken (either via the flag or an explicit ``source.platform=="llm"``
    override).  It holds the cached spec so ``content_config`` can forward the
    spec's ``content_selector`` to Firecrawl.

    Parameters
    ----------
    spec:
        Selector spec dict (as returned by ``derive_spec``).
    """

    name = "llm"

    def __init__(self, spec: dict) -> None:
        self._spec = spec

    def detect(self, root_html: str, root_url: str) -> bool:
        """Always False — DerivedProfile is never auto-selected by detection."""
        return False

    async def build_toc(self, root_url: str, scraper) -> list[TocEntry]:
        """Dispatch to the strategy indicated in the spec.

        Returns ``[]`` on any exception (resilience).
        """
        spec = self._spec
        strategy = spec.get("strategy", "sitemap")

        try:
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
                "DerivedProfile.build_toc failed for %s: %s", root_url, exc
            )
            return []

    def content_config(self) -> dict:
        """Return spec-aware content extraction config.

        When the LLM identified a ``content_selector`` use it for precise
        capture; otherwise fall back to the generic ``onlyMainContent`` heuristic.
        """
        selector = self._spec.get("content_selector")
        if selector and isinstance(selector, str) and selector.strip():
            return {
                "includeTags": [selector],
                "onlyMainContent": False,
                "waitFor": 1500,
            }
        return {"onlyMainContent": True, "waitFor": 1500}


class LlmProfile:
    """LLM-assisted profile for unrecognized documentation sites.

    Thin safety-net registered as ``"llm"`` so ``registry.get("llm")`` and UI
    selection continue to resolve.  The primary path for LLM-assisted extraction
    goes through ``_resolve_profile`` in ``firecrawl.py`` which caches the derived
    spec in ``source.profile_config["llm_spec"]`` and returns a ``DerivedProfile``
    directly.

    ``build_toc`` here is called only when the profile is used without a cached
    spec (e.g. direct/manual use or tests).  It scrapes the root HTML, calls
    ``derive_spec``, and delegates to ``DerivedProfile`` if a spec is returned.

    ``content_config`` returns the generic default because no cached spec is
    available at this call site.
    """

    name = "llm"

    def detect(self, root_html: str, root_url: str) -> bool:
        """Always returns False — LLM profile is never auto-selected."""
        return False

    async def build_toc(self, root_url: str, scraper) -> list[TocEntry]:
        """Ask the LLM which strategy to use, then dispatch to it.

        Returns ``[]`` when:
        - ``settings.llm_fallback_enabled`` is False (silent no-op gate).
        - ``derive_spec`` returns None (missing key, network error, etc.).
        - The strategy dispatch raises any exception (resilience).
        """
        if not settings.llm_fallback_enabled:
            return []

        try:
            html = await scraper.get_html(root_url)
            spec = await derive_spec(html, root_url)
            if spec is None:
                return []
            return await DerivedProfile(spec).build_toc(root_url, scraper)

        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "LLM fallback profile failed for %s: %s", root_url, exc
            )
            return []

    def content_config(self) -> dict:
        """Generic content-extraction config (no cached spec available here).

        ``onlyMainContent=True`` with a modest ``waitFor`` is a reasonable
        default for unrecognized sites.  When a spec is cached in
        ``source.profile_config``, ``_resolve_profile`` returns a
        ``DerivedProfile`` whose ``content_config`` is spec-aware.
        """
        return {"onlyMainContent": True, "waitFor": 1500}


PROFILE = LlmProfile()
registry.register(PROFILE)
