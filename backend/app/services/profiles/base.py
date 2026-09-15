"""Extraction-profile interface and the ordered TOC entry it produces."""

from dataclasses import dataclass
from typing import Protocol


@dataclass
class TocEntry:
    title: str
    url: str
    level: int
    is_article: bool = True
    parent_url: str | None = None
    # Optional CSS selector for this entry's content, overriding the profile's
    # run-wide selector — lets one page yield several section documents.
    content_selector: str | None = None
    # Optional URL to fetch for this entry's body on the raw_http path, when it
    # differs from the human-facing ``url`` (e.g. an API endpoint that returns
    # the article HTML as JSON). Defaults to ``url`` when None.
    content_url: str | None = None


class ExtractionProfile(Protocol):
    """A documentation platform's extraction strategy.

    Profiles only decide how the ordered TOC is built and which DOM is content;
    the rest of the pipeline (worker queue, changeTracking, images, incremental)
    is profile-agnostic.
    """

    name: str

    def detect(self, root_html: str, root_url: str) -> bool: ...

    async def build_toc(self, root_url: str, scraper) -> list[TocEntry]: ...

    def content_config(self) -> dict: ...

    # ── Optional: volatile URL tokens ({rev}) ───────────────────────────────
    # Both are looked up with getattr() and skipped when absent, so only a
    # platform whose URLs carry a per-release token besides the version needs to
    # implement them. See app/services/versioning.py for what {rev} is and why
    # key derivation deliberately does NOT depend on either of these.
    #
    # def templatize_url(self, url: str, version: str) -> str | None:
    #     """Return *url* as a url_template, with the platform's volatile token
    #     marked ``{rev}`` as well as the version marked ``{version}``. None when
    #     *url* doesn't fit the platform's URL grammar."""
    #
    # async def resolve_revision(self, template, version, scraper) -> str | None:
    #     """Return the live ``{rev}`` value for *version*, so a source's
    #     base_url can be rebuilt for a release it has never been crawled at.
    #     May hit the network; returning None must stay survivable."""
