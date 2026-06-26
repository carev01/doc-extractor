"""Realm session lifecycle — ensure a usable Browserless profile exists."""

import logging
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth_realm import AuthRealm, RealmStatus
from app.services.auth import login_scripts
from app.services.browserless import BrowserlessError, browserless_client

logger = logging.getLogger(__name__)


class NeedsLoginError(Exception):
    """A realm has no usable session and cannot be logged in without a human."""


async def ensure_profile(db: AsyncSession, realm: AuthRealm) -> str:
    """Return a usable Browserless profile name for `realm`, logging in via
    scripted creds if needed. Raises NeedsLoginError when a human is required."""
    if realm.status == RealmStatus.ACTIVE:
        return realm.browserless_profile_name
    if realm.username and realm.password:
        await run_scripted_login(db, realm)
        return realm.browserless_profile_name
    await invalidate(db, realm, RealmStatus.NEEDS_LOGIN,
                     "No stored credentials; assisted login required")
    raise NeedsLoginError(f"Realm {realm.id} needs an assisted login")


async def run_scripted_login(db: AsyncSession, realm: AuthRealm) -> None:
    """Run a headless scripted login; persist snapshot + ACTIVE, or LOGIN_FAILED."""
    code, context = login_scripts.build_login(realm)
    try:
        result = await browserless_client.run_login(code, context)
    except BrowserlessError as exc:
        await invalidate(db, realm, RealmStatus.LOGIN_FAILED, str(exc))
        raise NeedsLoginError(f"Scripted login failed for realm {realm.id}") from exc
    if not result.get("ok"):
        await invalidate(db, realm, RealmStatus.LOGIN_FAILED,
                         f"Login did not complete (finalUrl={result.get('finalUrl')})")
        raise NeedsLoginError(f"Scripted login did not complete for realm {realm.id}")
    realm.state_snapshot = result.get("state")
    realm.status = RealmStatus.ACTIVE
    realm.last_login_at = datetime.now(timezone.utc)
    realm.error_message = None
    await db.flush()


async def invalidate(db: AsyncSession, realm: AuthRealm, status: RealmStatus,
                     message: str | None = None) -> None:
    realm.status = status
    realm.error_message = message
    await db.flush()
