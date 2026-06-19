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


class FakeScraper:
    """Test double: serves canned HTML per URL and a canned URL list."""

    def __init__(
        self,
        html_by_url: dict[str, str],
        urls: list[str] | None = None,
        raw_by_url: dict[str, str] | None = None,
    ):
        self._h = html_by_url
        self._urls = urls or list(html_by_url)
        self._raw = raw_by_url or {}

    async def get_html(self, url: str, wait_ms: int = 1500) -> str:
        return self._h.get(url, "")

    async def get_raw(self, url: str) -> str:
        if url not in self._raw:
            raise FileNotFoundError(url)
        return self._raw[url]

    async def map_urls(self, root_url: str) -> list[str]:
        return list(self._urls)
