"""Pure unit tests for _dedupe_toc_entries — no DB required."""

import pytest
from app.services.firecrawl import _dedupe_toc_entries


def _make_section(title: str, level: int = 0) -> dict:
    return {"title": title, "url": "", "level": level, "is_article": False}


def _make_article(title: str, url: str, level: int = 1) -> dict:
    return {"title": title, "url": url, "level": level, "is_article": True}


def test_section_headers_are_never_collapsed():
    """Two url-less section headers both survive deduplication."""
    entries = [
        _make_section("S1"),
        _make_article("Article A", "https://docs.example.com/a"),
        _make_section("S2"),
        _make_article("Article B", "https://docs.example.com/b"),
    ]
    result = _dedupe_toc_entries(entries)
    titles = [e["title"] for e in result]
    assert "S1" in titles
    assert "S2" in titles


def test_duplicate_article_urls_are_dropped():
    """Second occurrence of an article URL is dropped, first is kept."""
    url = "https://docs.example.com/page"
    entries = [
        _make_article("Page (first)", url),
        _make_article("Page (duplicate)", url),
        _make_article("Other", "https://docs.example.com/other"),
    ]
    result = _dedupe_toc_entries(entries)
    urls = [e["url"] for e in result if e["url"]]
    assert urls.count(url) == 1
    # First occurrence is kept
    assert result[0]["title"] == "Page (first)"


def test_order_is_preserved():
    """DFS order of entries is preserved after deduplication."""
    entries = [
        _make_section("S1"),
        _make_article("A", "https://docs.example.com/a"),
        _make_article("B", "https://docs.example.com/b"),
        _make_section("S2"),
        _make_article("C", "https://docs.example.com/c"),
        _make_article("A-dup", "https://docs.example.com/a"),  # duplicate — dropped
    ]
    result = _dedupe_toc_entries(entries)
    titles = [e["title"] for e in result]
    assert titles == ["S1", "A", "B", "S2", "C"]


def test_sort_order_is_sequential():
    """sort_order values are 0..n-1 with no gaps."""
    entries = [
        _make_section("S1"),
        _make_article("A", "https://docs.example.com/a"),
        _make_section("S2"),
        _make_article("B", "https://docs.example.com/b"),
        _make_article("A-dup", "https://docs.example.com/a"),  # dropped
    ]
    result = _dedupe_toc_entries(entries)
    orders = [e["sort_order"] for e in result]
    assert orders == list(range(len(result)))


def test_scrape_set_excludes_url_less_sections():
    """Filtering result to entries with a url excludes section headers."""
    entries = [
        _make_section("S1"),
        _make_article("A", "https://docs.example.com/a"),
        _make_section("S2"),
        _make_article("B", "https://docs.example.com/b"),
    ]
    result = _dedupe_toc_entries(entries)
    scrape_set = [e for e in result if e["url"]]
    assert len(scrape_set) == 2
    assert all(e["url"] for e in scrape_set)
    # None of the section headers appear in the scrape set
    section_titles = {e["title"] for e in result if not e["url"]}
    scrape_titles = {e["title"] for e in scrape_set}
    assert section_titles.isdisjoint(scrape_titles)


def test_empty_input_returns_empty():
    assert _dedupe_toc_entries([]) == []


def test_all_sections_no_articles():
    """A list of only section headers all survive."""
    entries = [_make_section(f"S{i}") for i in range(3)]
    result = _dedupe_toc_entries(entries)
    assert len(result) == 3
    assert [e["sort_order"] for e in result] == [0, 1, 2]
