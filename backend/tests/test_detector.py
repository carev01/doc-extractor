"""Tests for the platform detector (app.services.profiles.detector).

Profiles are tested in their own test_profiles_<name>.py files (Tasks 6-14).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.profiles.detector import detect_platform
from app.services.profiles import registry

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "platforms")
LAZY_TREE_ROOT = "https://documentation.commvault.com/clumio/index.html"


def _read(name: str) -> str:
    return open(os.path.join(FIXTURE_DIR, name), encoding="utf-8").read()


def test_lazy_tree_fixture_detects_as_lazy_tree():
    html = _read("lazy_tree.html")
    assert detect_platform(html, LAZY_TREE_ROOT) == "lazy_tree"


def test_junk_html_returns_none():
    junk = "<html><body><p>Nothing here that matches any platform.</p></body></html>"
    assert detect_platform(junk, "https://example.com/") is None


def test_empty_html_returns_none():
    assert detect_platform("", "https://example.com/") is None


def test_generic_profile_exists_in_registry():
    """The generic profile must be registered so _resolve_profile fallback works."""
    p = registry.get("generic")
    assert p is not None
    assert p.name == "generic"
