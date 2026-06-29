"""Realm session-expiry helpers, derived from the stored cookie snapshot."""

from datetime import datetime, timezone


def session_expires_at(realm) -> datetime | None:
    """Soonest expiry among the realm's stored cookies that have one, as aware
    UTC. ``None`` when the snapshot is empty or all cookies are session-only."""
    snapshot = getattr(realm, "state_snapshot", None) or {}
    expiries = [
        c["expires"]
        for c in snapshot.get("cookies", [])
        if isinstance(c.get("expires"), (int, float)) and not isinstance(c.get("expires"), bool) and c["expires"] > 0
    ]
    if not expiries:
        return None
    return datetime.fromtimestamp(min(expiries), tz=timezone.utc)


def session_expired(realm) -> bool:
    """True when the realm's soonest cookie expiry is in the past."""
    exp = session_expires_at(realm)
    return exp is not None and exp <= datetime.now(timezone.utc)
