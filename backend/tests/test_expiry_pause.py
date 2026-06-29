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


import pytest  # noqa: E402


@pytest.mark.asyncio
async def test_pause_for_expiry_invalidates_notifies_and_raises(monkeypatch):
    import app.services.firecrawl as fc

    calls = {}

    async def fake_invalidate(db, realm, status, msg):
        calls["invalidate"] = (realm.name, status)

    async def fake_notify(title, message, **fields):
        calls["notify"] = (title, fields.get("realm"))

    monkeypatch.setattr(fc.realm_manager, "invalidate", fake_invalidate)
    monkeypatch.setattr(fc, "notify", fake_notify)

    class DB:
        async def commit(self):
            calls["commit"] = True

    realm = types.SimpleNamespace(name="Rubrik Docs")
    src = types.SimpleNamespace(name="RSC SaaS Docs")

    with pytest.raises(fc.RunControlSignal) as ei:
        await fc._pause_for_expiry(DB(), src, realm)

    assert ei.value.action == "pause"
    assert calls["invalidate"] == ("Rubrik Docs", fc.RealmStatus.EXPIRED)
    assert calls["notify"][1] == "Rubrik Docs"
    assert calls.get("commit") is True
