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

    async def map_urls(self, root_url: str) -> list[str]:
        return await self._fc.map_urls(root_url)


class FakeScraper:
    """Test double: serves canned HTML per URL and a canned URL list."""

    def __init__(self, html_by_url: dict[str, str], urls: list[str] | None = None):
        self._h = html_by_url
        self._urls = urls or list(html_by_url)

    async def get_html(self, url: str, wait_ms: int = 1500) -> str:
        return self._h.get(url, "")

    async def map_urls(self, root_url: str) -> list[str]:
        return list(self._urls)
