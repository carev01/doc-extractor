"""_is_auth_expiry returns True for any non-None realm and False for None.

A mass raw-HTTP failure on a source that has an auth realm is treated as a
likely session/WAF failure → pause+EXPIRED+notify, regardless of whether the
cookie's timestamp has technically expired (server-side / WAF session death
keeps the cookie timestamp valid). Only unauthenticated sources (realm=None)
fail loudly via RawContentScrapeError.
"""

import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.firecrawl import _is_auth_expiry


def test_auth_expiry_true_for_any_realm():
    realm = types.SimpleNamespace(name="test-realm")
    assert _is_auth_expiry(realm) is True


def test_not_auth_expiry_without_realm():
    assert _is_auth_expiry(None) is False
