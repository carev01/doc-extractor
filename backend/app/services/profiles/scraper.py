"""Scraper adapter so profiles depend on a small interface, not FirecrawlService.

This keeps profiles unit-testable: tests pass a FakeScraper that serves fixture
HTML, with no network.
"""


class Scraper:
    """Thin adapter over FirecrawlService for use by extraction profiles.

    ``checkpoint`` (a TocBuildCheckpoint or None) lets a profile persist
    incremental TOC-build progress so a long expansion can resume after an
    interruption; profiles that don't need it ignore it.
    """

    def __init__(self, firecrawl, checkpoint=None):
        self._fc = firecrawl
        self.checkpoint = checkpoint

    async def get_html(self, url: str, wait_ms: int = 1500) -> str:
        data = await self._fc._firecrawl_request(
            url,
            {"formats": ["html"], "onlyMainContent": False, "waitFor": wait_ms},
        )
        return data.get("html", "")

    async def get_raw(self, url: str) -> str:
        """Verbatim GET of a static asset (e.g. Flare Data/*.js|xml TOC files)."""
        return await self._fc.fetch_raw(url)

    async def map_urls(self, root_url: str) -> list[str]:
        return await self._fc.map_urls(root_url)

    async def render(self, url: str) -> dict:
        """Render via Browserless (real Chrome) and extract shadow-DOM content.

        Returns {toc, contentHtml, contentText, title} — used by profiles whose
        content lives in shadow DOM (e.g. Salesforce Help) that Firecrawl can't
        serialise.
        """
        from app.services.browserless import browserless_client
        return await browserless_client.render(url)

    async def get_rendered_html(self, url: str, wait_for: str | None = None) -> str:
        """Fully-rendered light-DOM HTML via Browserless, after ``wait_for``
        appears — for navs/content built client-side (e.g. a client-built #nav)."""
        from app.services.browserless import browserless_client
        return await browserless_client.render_html(url, wait_selector=wait_for)

    async def expand_toc(self, url: str, section_id: str | None = None) -> list[dict]:
        """Depth-first expand a lazy sidebar tree via Browserless; returns ordered
        {href, title, level, isParent} nodes (clicks every parent toggle)."""
        from app.services.browserless import browserless_client
        return await browserless_client.expand_toc(url, section_id=section_id)

    async def gitbook_sidebars(self, urls: list[str]) -> dict[str, str]:
        """Visit each URL via Browserless and return {url: table-of-contents HTML}
        — for reconstructing a GitBook tree whose sidebar is contextual."""
        from app.services.browserless import browserless_client
        return await browserless_client.gitbook_sidebars(urls)

    async def expand_docusaurus_sidebar(self, url: str) -> str:
        """Fully expand a Docusaurus sidebar via Browserless and return the
        ``.theme-doc-sidebar-menu`` HTML with every collapsed category mounted —
        Docusaurus doesn't render category children until expanded."""
        from app.services.browserless import browserless_client
        return await browserless_client.expand_docusaurus_sidebar(url)

    async def expand_collapsible_sidebar(self, url: str) -> str:
        """Fully expand a shadcn/ui + radix Collapsible sidebar via Browserless
        and return the ``[data-slot='sidebar-inner']`` HTML with
        every collapsed node mounted — children aren't in the DOM until expanded."""
        from app.services.browserless import browserless_client
        return await browserless_client.expand_collapsible_sidebar(url)

    async def warmup_render(self, url: str, selector: str | None = None,
                            warmup_url: str | None = None) -> dict:
        """Render via Browserless after a warm-up navigation (to clear a WAF such
        as Akamai), returning ``{outerHtml, innerHtml, title}`` for ``selector`` —
        used by the warmup_listgroup profile for both its CSS-collapsed TOC and its
        article bodies, neither reachable via a cold Firecrawl scrape."""
        from app.services.browserless import browserless_client
        return await browserless_client.warmup_render(
            url, selector=selector, warmup_url=warmup_url
        )


class FakeScraper:
    """Test double: serves canned HTML per URL and a canned URL list."""

    def __init__(
        self,
        html_by_url: dict[str, str],
        urls: list[str] | None = None,
        raw_by_url: dict[str, str] | None = None,
        render_by_url: dict[str, dict] | None = None,
        rendered_html_by_url: dict[str, str] | None = None,
        toc_by_url: dict[str, list] | None = None,
        gitbook_sidebars_by_url: dict[str, str] | None = None,
        docusaurus_sidebar_by_url: dict[str, str] | None = None,
        collapsible_sidebar_by_url: dict[str, str] | None = None,
        warmup_render_by_url: dict[str, dict] | None = None,
        checkpoint=None,
    ):
        self._h = html_by_url
        self._urls = urls or list(html_by_url)
        self._raw = raw_by_url or {}
        self._render = render_by_url or {}
        self._rendered_html = rendered_html_by_url or {}
        self._toc = toc_by_url or {}
        self._gitbook = gitbook_sidebars_by_url or {}
        self._docusaurus = docusaurus_sidebar_by_url or {}
        self._collapsible = collapsible_sidebar_by_url or {}
        self._warmup = warmup_render_by_url or {}
        self.checkpoint = checkpoint

    async def get_html(self, url: str, wait_ms: int = 1500) -> str:
        return self._h.get(url, "")

    async def get_raw(self, url: str) -> str:
        if url not in self._raw:
            raise FileNotFoundError(url)
        return self._raw[url]

    async def map_urls(self, root_url: str) -> list[str]:
        return list(self._urls)

    async def render(self, url: str) -> dict:
        return self._render.get(url, {})

    async def get_rendered_html(self, url: str, wait_for: str | None = None) -> str:
        return self._rendered_html.get(url, "")

    async def expand_toc(self, url: str, section_id: str | None = None) -> list[dict]:
        # Keyed by section_id when given (so the full-mode "__TOP__" + per-section
        # orchestration is testable), else by url.
        return self._toc.get(section_id if section_id else url, [])

    async def gitbook_sidebars(self, urls: list[str]) -> dict[str, str]:
        return {u: self._gitbook[u] for u in urls if u in self._gitbook}

    async def expand_docusaurus_sidebar(self, url: str) -> str:
        from app.services.browserless import BrowserlessError
        if url not in self._docusaurus:
            raise BrowserlessError(f"no docusaurus sidebar fixture for {url}")
        return self._docusaurus[url]

    async def expand_collapsible_sidebar(self, url: str) -> str:
        from app.services.browserless import BrowserlessError
        if url not in self._collapsible:
            raise BrowserlessError(f"no collapsible sidebar fixture for {url}")
        return self._collapsible[url]

    async def warmup_render(self, url: str, selector: str | None = None,
                            warmup_url: str | None = None) -> dict:
        from app.services.browserless import BrowserlessError
        if url not in self._warmup:
            raise BrowserlessError(f"no warmup_render fixture for {url}")
        return self._warmup[url]
