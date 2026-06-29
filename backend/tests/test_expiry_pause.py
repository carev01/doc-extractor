"""_is_auth_expiry decides whether a mass raw-HTTP scrape failure is an expired
auth session (→ pause + notify) vs a genuine failure (→ fail loudly)."""

import os
import sys
import types
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.firecrawl import _is_auth_expiry


def _realm(exp):
    return types.SimpleNamespace(
        state_snapshot={"cookies": [{"name": "t", "expires": exp}], "origins": []}
    )


def test_auth_expiry_true_for_expired_realm():
    past = datetime.now(timezone.utc).timestamp() - 10
    assert _is_auth_expiry(_realm(past)) is True


def test_not_auth_expiry_when_realm_live():
    future = datetime.now(timezone.utc).timestamp() + 10000
    assert _is_auth_expiry(_realm(future)) is False


def test_not_auth_expiry_without_realm():
    assert _is_auth_expiry(None) is False


def test_not_auth_expiry_for_session_only_cookies():
    # No positive expiry → session_expired is False → not treated as expiry.
    realm = types.SimpleNamespace(state_snapshot={"cookies": [{"name": "t"}], "origins": []})
    assert _is_auth_expiry(realm) is False
