import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from app.services.profiles.scraper import Scraper


class _FC:
    def __init__(self):
        self.calls = []

    async def fetch_raw(self, url, cookies=None):
        self.calls.append((url, cookies))
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
