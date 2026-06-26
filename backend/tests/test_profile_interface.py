"""Every registered profile must satisfy the ExtractionProfile interface.

extract_source calls profile.content_config() unconditionally (before the
content-engine branch), so a profile shipping without it crashes the whole run
at runtime — which is exactly how the zoomin profile broke Zerto extraction
('ZoominProfile' object has no attribute 'content_config'). This guard fails at
test time instead, for every profile, present and future.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import inspect

import pytest

import app.services.profiles  # noqa: F401 — registers all built-in profiles
from app.services.profiles import registry


def _profile_ids():
    return [getattr(p, "name", p.__class__.__name__) for p in registry.PROFILES]


@pytest.mark.parametrize("profile", registry.PROFILES, ids=_profile_ids())
def test_profile_satisfies_interface(profile):
    # name
    assert isinstance(getattr(profile, "name", None), str) and profile.name

    # required callables
    for method in ("detect", "build_toc", "content_config"):
        assert callable(getattr(profile, method, None)), (
            f"{profile.name} is missing {method}()"
        )

    # content_config is called eagerly by extract_source — it must return a dict
    # without arguments and without raising.
    cfg = profile.content_config()
    assert isinstance(cfg, dict), f"{profile.name}.content_config() must return a dict"

    # detect must be sync and return a bool for a trivial input.
    assert not inspect.iscoroutinefunction(profile.detect), f"{profile.name}.detect must be sync"
    assert isinstance(profile.detect("<html></html>", "https://example.com/"), bool)

    # build_toc must be async (it awaits the scraper).
    assert inspect.iscoroutinefunction(profile.build_toc), f"{profile.name}.build_toc must be async"
