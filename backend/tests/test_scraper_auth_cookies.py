import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from app.services.profiles.scraper import Scraper


class _FC:
    def __init__(self):
        self.calls = []

    # Mirrors FirecrawlService.fetch_raw's signature — including user_agent, which
    # Scraper forwards so a profile's raw_user_agent reaches its TOC fetches too.
    async def fetch_raw(self, url, cookies=None, retry_statuses=None, user_agent=None):
        self.calls.append((url, cookies))
        self.user_agents = getattr(self, "user_agents", [])
        self.user_agents.append(user_agent)
        return "<html>ok</html>"


@pytest.mark.asyncio
async def test_get_raw_forwards_auth_cookies():
    fc = _FC()
    cookies = [{"name": "SAML", "value": "tok"}]
    s = Scraper(fc, auth_cookies=cookies)
    await s.get_raw("https://x/p")
    assert fc.calls == [("https://x/p", cookies)]


@pytest.mark.asyncio
async def test_get_raw_default_no_cookies():
    fc = _FC()
    s = Scraper(fc)
    await s.get_raw("https://x/p")
    assert fc.calls == [("https://x/p", None)]


@pytest.mark.asyncio
async def test_get_raw_forwards_profile_user_agent():
    """A profile whose platform rejects the default browser UA (dita_api: IBM's
    edge 403s it) declares raw_user_agent; the Scraper must carry it into the TOC
    fetch, not just the content path."""
    fc = _FC()
    s = Scraper(fc, user_agent="curl/8.7.1 (+DocExtractor)")
    await s.get_raw("https://x/p")
    assert fc.user_agents == ["curl/8.7.1 (+DocExtractor)"]


@pytest.mark.asyncio
async def test_get_raw_user_agent_defaults_to_none():
    # None means "no override" — fetch_raw then sends the default browser UA.
    fc = _FC()
    s = Scraper(fc)
    await s.get_raw("https://x/p")
    assert fc.user_agents == [None]
