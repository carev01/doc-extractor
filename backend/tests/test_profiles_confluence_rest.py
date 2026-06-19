"""Tests for the Confluence REST-API hierarchy builder.

The profile builds the full, ordered page-tree from
``/rest/api/content?...&expand=ancestors,extensions.position``: a flat list of
pages where the last ancestor is the parent and ``extensions.position`` is the
sibling sort key.  These tests feed canned REST JSON through FakeScraper's
``raw_by_url`` and assert the reconstructed hierarchy (count, depth, order,
parent linkage), plus graceful fallback to link-scraping when REST is absent.
"""

import json

import pytest

from app.services.profiles.confluence import ConfluenceProfile
from app.services.profiles.scraper import FakeScraper

WIKI = "https://docs.example.com/wiki"
ROOT = f"{WIKI}/spaces/BCCB/overview?homepageId=100"
REST_URL = (
    f"{WIKI}/rest/api/content?spaceKey=BCCB&type=page"
    "&expand=ancestors,extensions.position&limit=100&start=0"
)


def _page(pid, title, ancestors, position):
    return {
        "id": pid,
        "title": title,
        "ancestors": [{"id": a} for a in ancestors],
        "extensions": {"position": position},
        "_links": {"webui": f"/spaces/BCCB/pages/{pid}/{title.replace(' ', '+')}"},
    }


# A small tree:
#   Overview (100)
#     ├─ Release Notes (101)          pos 50
#     ├─ Understanding (102)          pos 200
#     │     ├─ Data M365 (103)        pos 30
#     │     └─ Data Entra (104)       pos 60
#     └─ Troubleshooting (105)        pos 300
# Sibling order is by position asc, regardless of input order.
REST_BODY = json.dumps({
    "results": [
        _page(104, "Data Entra", [100, 102], 60),
        _page(100, "Overview", [], 0),
        _page(105, "Troubleshooting", [100], 300),
        _page(102, "Understanding", [100], 200),
        _page(101, "Release Notes", [100], 50),
        _page(103, "Data M365", [100, 102], 30),
    ],
    "_links": {},  # no "next" → single page
})


@pytest.mark.asyncio
async def test_rest_builds_full_ordered_hierarchy():
    scraper = FakeScraper({}, raw_by_url={REST_URL: REST_BODY})
    toc = await ConfluenceProfile().build_toc(ROOT, scraper)

    # Depth-first, siblings ordered by position: the missed-children bug would
    # have dropped 103/104 entirely.
    got = [(e.title, e.level) for e in toc]
    assert got == [
        ("Overview", 0),
        ("Release Notes", 1),
        ("Understanding", 1),
        ("Data M365", 2),
        ("Data Entra", 2),
        ("Troubleshooting", 1),
    ]


@pytest.mark.asyncio
async def test_rest_parent_links_and_absolute_urls():
    scraper = FakeScraper({}, raw_by_url={REST_URL: REST_BODY})
    toc = await ConfluenceProfile().build_toc(ROOT, scraper)
    by_title = {e.title: e for e in toc}

    # Every URL absolute and rooted at the /wiki context path.
    for e in toc:
        assert e.url.startswith(f"{WIKI}/spaces/BCCB/pages/")

    # Children point at their parent's URL; roots have None.
    assert by_title["Overview"].parent_url is None
    assert by_title["Release Notes"].parent_url == by_title["Overview"].url
    assert by_title["Data M365"].parent_url == by_title["Understanding"].url
    assert by_title["Data Entra"].parent_url == by_title["Understanding"].url


@pytest.mark.asyncio
async def test_rest_pagination_follows_next():
    """Two REST pages stitched together via the start cursor + _links.next."""
    page1 = json.dumps({
        "results": [_page(100, "Overview", [], 0), _page(101, "A", [100], 10)],
        "_links": {"next": "/rest/api/content?...&start=100"},
    })
    page2_url = (
        f"{WIKI}/rest/api/content?spaceKey=BCCB&type=page"
        "&expand=ancestors,extensions.position&limit=100&start=100"
    )
    page2 = json.dumps({
        "results": [_page(102, "B", [100], 20)],
        "_links": {},
    })
    scraper = FakeScraper({}, raw_by_url={REST_URL: page1, page2_url: page2})
    toc = await ConfluenceProfile().build_toc(ROOT, scraper)
    assert [e.title for e in toc] == ["Overview", "A", "B"]


@pytest.mark.asyncio
async def test_falls_back_to_link_scraping_when_rest_unavailable():
    """No REST JSON served (get_raw raises) → flat level-0 link scrape."""
    html = (
        '<div class="wiki-content">'
        '<a href="/wiki/spaces/BCCB/pages/3244108/Release+Notes">Release Notes</a>'
        '<a href="/wiki/spaces/BCCB/pages/3244112/Troubleshooting">Troubleshooting</a>'
        "</div>"
    )
    scraper = FakeScraper({ROOT: html})  # no raw_by_url → REST path fails
    toc = await ConfluenceProfile().build_toc(ROOT, scraper)
    assert [e.title for e in toc] == ["Release Notes", "Troubleshooting"]
    assert all(e.level == 0 for e in toc)
