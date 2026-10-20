"""Realm session-expiry helpers, derived from the stored cookie snapshot."""

import re
from datetime import datetime, timezone


# Cookies that carry the login itself. A snapshot also holds analytics, consent,
# bot-management and correlation cookies, and those expire on their own clock:
# AvePoint's `correlation_id` lapsed 40 minutes after a fresh login while its
# `session_token` had another hour and its `refresh_token` a week, so "soonest of
# any cookie" called a working session expired — and the trigger routes 409 on that.
# Long-lived tracking cookies (posthog/beamer/consent, months out) are excluded for
# the opposite reason: a max() over them would call a dead session alive.
_CREDENTIAL_COOKIE_RE = re.compile(
    r"sess|token|auth|saml|jwt|(^|[._-])sid$|signature|policy|key-pair", re.IGNORECASE)
# CSRF tokens match "token" but guard forms, not the login, and some stacks
# (Laravel's XSRF-TOKEN) re-mint them every couple of hours.
_NOT_CREDENTIAL_RE = re.compile(r"csrf|xsrf", re.IGNORECASE)


def _expiry(c: dict) -> float | None:
    e = c.get("expires")
    if isinstance(e, (int, float)) and not isinstance(e, bool) and e > 0:
        return e
    return None


def is_credential_cookie(name: str) -> bool:
    return bool(_CREDENTIAL_COOKIE_RE.search(name)) and not _NOT_CREDENTIAL_RE.search(name)


def session_expires_at(realm) -> datetime | None:
    """Soonest expiry among the realm's stored *credential* cookies, as aware UTC.

    Falls back to every cookie when none is recognisably a credential, so an
    unfamiliar site keeps the conservative soonest-of-all reading. ``None`` when
    the snapshot is empty or the relevant cookies are all session-only."""
    snapshot = getattr(realm, "state_snapshot", None) or {}
    cookies = [c for c in snapshot.get("cookies", []) if isinstance(c, dict)]
    creds = [c for c in cookies if is_credential_cookie(str(c.get("name", "")))]
    expiries = [e for c in (creds or cookies) if (e := _expiry(c)) is not None]
    if not expiries:
        return None
    return datetime.fromtimestamp(min(expiries), tz=timezone.utc)


def session_expired(realm) -> bool:
    """True when the realm's soonest cookie expiry is in the past."""
    exp = session_expires_at(realm)
    return exp is not None and exp <= datetime.now(timezone.utc)
