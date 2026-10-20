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


def test_mixed_session_and_persistent_cookies():
    """A -1 session sentinel mixed with a real future expiry must not poison min()."""
    future = datetime.now(timezone.utc).timestamp() + 10000
    realm = _realm([{"name": "sess", "expires": -1}, {"name": "auth", "expires": future}])
    exp = session_expires_at(realm)
    assert exp is not None, "should return the future expiry, not None"
    assert exp == datetime.fromtimestamp(future, tz=timezone.utc)
    assert session_expired(realm) is False


def test_only_negative_expiry_session_cookies():
    """A snapshot with only -1 session sentinels is treated as session-only (no expiry)."""
    realm = _realm([{"name": "sess", "expires": -1}])
    assert session_expires_at(realm) is None
    assert session_expired(realm) is False


def _at(hours):
    return datetime.now(timezone.utc).timestamp() + hours * 3600


def test_tracking_cookie_does_not_expire_a_live_session():
    """AvePoint, measured: correlation_id lapsed 40 min after login while the
    session_token had another hour — the realm read EXPIRED and triggers 409'd."""
    realm = _realm([
        {"name": "correlation_id", "expires": _at(-0.7)},
        {"name": "KBGateway-AutoLogin-Hint", "expires": _at(-0.7)},
        {"name": "KBGateway-User-Context", "expires": _at(0.1)},
        {"name": "session_token", "expires": _at(1.1)},
        {"name": "refresh_token", "expires": _at(167)},
    ])
    assert session_expired(realm) is False
    assert abs(session_expires_at(realm).timestamp() - _at(1.1)) < 5


def test_long_lived_tracking_cookie_does_not_keep_a_dead_session_alive():
    """Eon/Securiti, measured: the auth cookie died weeks ago, analytics run months out."""
    eon = _realm([{"name": "fern_token", "expires": _at(-594)},
                  {"name": "ph_phc_yQgAEdJJkVpI24NdSRID2mor1x1leRpDo_posthog", "expires": _at(3701)}])
    securiti = _realm([{"name": "__cf_bm", "expires": _at(-2115)},
                       {"name": "connect.sid", "expires": _at(-2091)},
                       {"name": "CloudFront-Signature", "expires": _at(-1947)},
                       {"name": "DOC_ENV", "expires": _at(6644)},
                       {"name": "_BEAMER_USER_ID_x", "expires": _at(5084)}])
    assert session_expired(eon) is True
    assert session_expired(securiti) is True


def test_csrf_token_is_not_a_credential():
    realm = _realm([{"name": "XSRF-TOKEN", "expires": _at(-1)},
                    {"name": "laravel_session", "expires": _at(2)}])
    assert session_expired(realm) is False


def test_effective_status_reports_lapsed_session_as_expired():
    from app.models.auth_realm import RealmStatus
    from app.routes.auth_realms import effective_status
    dead = _realm([{"name": "SAML_TOKEN", "expires": _at(-1)}])
    dead.status = RealmStatus.ACTIVE
    live = _realm([{"name": "SAML_TOKEN", "expires": _at(5)}])
    live.status = RealmStatus.ACTIVE
    failed = _realm([{"name": "SAML_TOKEN", "expires": _at(-1)}])
    failed.status = RealmStatus.LOGIN_FAILED
    assert effective_status(dead) == "expired"
    assert effective_status(live) == "active"
    assert effective_status(failed) == "login_failed"   # never masks a worse state
