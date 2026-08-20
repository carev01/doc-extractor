"""The dita_api (IBM Documentation) fetches must not send the default browser UA.

Regression: www.ibm.com's edge WAF answers a *browser* User-Agent with an instant
403 on both API endpoints — the toc API and every topic body — while allowing a
plain HTTP-client UA. Claiming to be Chrome invites browser-integrity checks a
header-less API GET can't pass, so the browser UA is the one that gets refused.

The failure was silent and misleading: build_toc swallowed the 403 and returned
[], extract_source replaced an empty TOC with a synthetic single-page "Index",
and the TOC-collapse guard then aborted the run reporting "1 scrapable page vs a
baseline of 328 ... likely a scraper or upstream (Firecrawl/Browserless) failure"
— blaming services this path never touches. Three IBM sources failed this way.

These tests pin the UA plumbing end to end (profile -> Scraper -> fetch_raw) and
simulate the WAF, so a regression fails here instead of as a guard trip in prod.
"""

import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.firecrawl import FirecrawlService, _BROWSER_UA
from app.services.profiles.dita_api import PROFILE as DITA
from app.services.profiles.scraper import Scraper

# A minimal toc-API payload shaped like the real one: a synthetic product root
# wrapper whose children are the real topics (one nested, to cover the walk).
_TOC_JSON = """
{"_id": "SS57AN_2.3.1",
 "toc": {"href": "SS57AN_2.3.1", "label": "Product", "topicId": "p",
         "topics": [
           {"topicId": "welcome", "href": "SS57AN_2.3.1/welcome.html", "label": "Welcome"},
           {"label": "Section", "href": "SS57AN_2.3.1",
            "topics": [{"topicId": "t2", "href": "SS57AN_2.3.1/t2.html", "label": "Topic Two"}]}
         ]}}
"""


def _waf_transport(seen: list) -> httpx.MockTransport:
    """Stands in for IBM's edge: 403 for a browser UA, 200 otherwise."""
    def handler(request: httpx.Request) -> httpx.Response:
        ua = request.headers.get("user-agent", "")
        seen.append(ua)
        if "Mozilla" in ua:
            return httpx.Response(403, text="<html>Access Denied</html>")
        return httpx.Response(200, text=_TOC_JSON)
    return httpx.MockTransport(handler)


def _service(seen: list) -> FirecrawlService:
    svc = FirecrawlService()
    svc.client = httpx.AsyncClient(transport=_waf_transport(seen))
    return svc


def test_profile_declares_a_non_browser_user_agent():
    ua = getattr(DITA, "raw_user_agent", None)
    assert ua, "dita_api must declare raw_user_agent or IBM 403s every fetch"
    assert "Mozilla" not in ua, "a browser UA is exactly what IBM's edge refuses"


@pytest.mark.asyncio
async def test_build_toc_succeeds_against_a_browser_ua_blocking_edge():
    """The whole point: with the profile's UA wired in, discovery survives the WAF."""
    seen: list[str] = []
    svc = _service(seen)
    scraper = Scraper(svc, user_agent=DITA.raw_user_agent)

    entries = await DITA.build_toc("https://www.ibm.com/docs/en/scdm/2.3.1", scraper)

    assert [e.title for e in entries] == ["Welcome", "Section", "Topic Two"]
    # Two real topics carry a URL; the structural node (no .html href) does not.
    assert sum(1 for e in entries if e.url) == 2
    assert all("Mozilla" not in ua for ua in seen)
    await svc.client.aclose()


@pytest.mark.asyncio
async def test_build_toc_without_the_override_collapses_to_nothing():
    """Guards the guard: the pre-fix behaviour that produced the bogus 1-page TOC.

    Without the UA override the fetch 403s, build_toc returns [] — which upstream
    turns into a single synthetic "Index" entry and a TOC-collapse abort.
    """
    seen: list[str] = []
    svc = _service(seen)
    scraper = Scraper(svc)  # no user_agent -> fetch_raw sends _BROWSER_UA

    entries = await DITA.build_toc("https://www.ibm.com/docs/en/scdm/2.3.1", scraper)

    assert entries == []
    assert seen and all(ua == _BROWSER_UA for ua in seen)
    await svc.client.aclose()


@pytest.mark.asyncio
async def test_fetch_raw_override_reaches_the_wire():
    seen: list[str] = []
    svc = _service(seen)
    await svc.fetch_raw("https://www.ibm.com/docs/api/v1/content/x.html", user_agent="curl/8.7.1")
    assert seen == ["curl/8.7.1"]
    await svc.client.aclose()


@pytest.mark.asyncio
async def test_fetch_raw_defaults_to_browser_ua():
    # Unchanged for every other profile: the override is opt-in per platform.
    seen: list[str] = []
    svc = _service(seen)
    with pytest.raises(httpx.HTTPStatusError):  # the fake WAF 403s it
        await svc.fetch_raw("https://www.ibm.com/docs/api/v1/content/x.html")
    assert seen == [_BROWSER_UA]
    await svc.client.aclose()


@pytest.mark.asyncio
async def test_image_download_honours_the_override():
    """Images live on the same blocked host as the pages, and a failed download
    returns None *silently* — so without the UA the article would be stored with
    every screenshot missing, and the run would still report success."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        ua = request.headers.get("user-agent", "")
        seen.append(ua)
        if "Mozilla" in ua:
            return httpx.Response(403, text="<html>Access Denied</html>")
        return httpx.Response(200, content=b"\xff\xd8\xff\xe0jpegbytes",
                              headers={"content-type": "image/jpeg"})

    svc = FirecrawlService()
    svc.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    img = "https://www.ibm.com/docs/en/SSESK4_6.3.18/images/warning16.jpg"

    import tempfile
    with tempfile.TemporaryDirectory() as d:
        assert await svc._download_image(img, d) is None          # browser UA: dropped
        assert await svc._download_image(img, d, user_agent=DITA.raw_user_agent)
    assert seen == [_BROWSER_UA, DITA.raw_user_agent]
    await svc.client.aclose()


@pytest.mark.asyncio
async def test_fetch_raw_override_applies_on_the_cookie_redirect_path():
    """The cookie branch builds its own header dict — it must honour the override
    too, or an authenticated dita_api-style source would still send Mozilla."""
    seen: list[str] = []
    svc = _service(seen)
    await svc.fetch_raw(
        "https://www.ibm.com/docs/api/v1/content/x.html",
        cookies=[{"name": "s", "value": "1"}],
        user_agent="curl/8.7.1",
    )
    assert seen == ["curl/8.7.1"]
    await svc.client.aclose()
