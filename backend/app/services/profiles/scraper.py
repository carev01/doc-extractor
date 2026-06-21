"""Scraper adapter so profiles depend on a small interface, not FirecrawlService.

This keeps profiles unit-testable: tests pass a FakeScraper that serves fixture
HTML, with no network.
"""


class Scraper:
    """Thin adapter over FirecrawlService for use by extraction profiles."""

    def __init__(self, firecrawl):
        self._fc = firecrawl

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
        appears — for navs/content built client-side (e.g. Commvault's #nav)."""
        from app.services.browserless import browserless_client
        return await browserless_client.render_html(url, wait_selector=wait_for)


class FakeScraper:
    """Test double: serves canned HTML per URL and a canned URL list."""

    def __init__(
        self,
        html_by_url: dict[str, str],
        urls: list[str] | None = None,
        raw_by_url: dict[str, str] | None = None,
        render_by_url: dict[str, dict] | None = None,
        rendered_html_by_url: dict[str, str] | None = None,
    ):
        self._h = html_by_url
        self._urls = urls or list(html_by_url)
        self._raw = raw_by_url or {}
        self._render = render_by_url or {}
        self._rendered_html = rendered_html_by_url or {}

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
