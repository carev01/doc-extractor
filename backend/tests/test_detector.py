"""Tests for the platform detector (app.services.profiles.detector).

Only the commvault profile is registered at this point.  Other platform
profiles are tested in their own test_profiles_<name>.py files (Tasks 6-13).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.profiles.detector import detect_platform

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "platforms")
COMMVAULT_ROOT = "https://documentation.commvault.com/clumio/index.html"


def _read(name: str) -> str:
    return open(os.path.join(FIXTURE_DIR, name), encoding="utf-8").read()


def test_commvault_fixture_detects_as_commvault():
    html = _read("commvault.html")
    assert detect_platform(html, COMMVAULT_ROOT) == "commvault"


def test_junk_html_returns_none():
    junk = "<html><body><p>Nothing here that matches any platform.</p></body></html>"
    assert detect_platform(junk, "https://example.com/") is None


def test_empty_html_returns_none():
    assert detect_platform("", "https://example.com/") is None
