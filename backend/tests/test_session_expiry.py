import os
import sys
import types
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.auth.session import session_expires_at, session_expired


def _realm(cookies):
    return types.SimpleNamespace(state_snapshot={"cookies": cookies, "origins": []})


def test_soonest_expiry_chosen():
    exp = session_expires_at(_realm([{"name": "a", "expires": 2000000000},
                                     {"name": "b", "expires": 1900000000}]))
    assert exp == datetime.fromtimestamp(1900000000, tz=timezone.utc)


def test_none_when_no_expiring_cookies():
    assert session_expires_at(_realm([{"name": "s"}])) is None
    assert session_expires_at(types.SimpleNamespace(state_snapshot=None)) is None


def test_session_expired_flag():
    past = datetime.now(timezone.utc).timestamp() - 10
    future = datetime.now(timezone.utc).timestamp() + 10000
    assert session_expired(_realm([{"name": "a", "expires": past}])) is True
    assert session_expired(_realm([{"name": "a", "expires": future}])) is False
    assert session_expired(_realm([{"name": "s"}])) is False  # no expiry → not expired
