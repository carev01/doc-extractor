"""Realm session lifecycle — ensure a usable auth state dict exists."""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth_realm import AuthRealm, RealmStatus
from app.models.extraction_run import ExtractionRun
from app.models.job_run import JobRun
from app.services.auth import login_scripts
from app.services.auth.session import session_expires_at
from app.services.browserless import BrowserlessError, browserless_client

logger = logging.getLogger(__name__)


class NeedsLoginError(Exception):
    """A realm has no usable session and cannot be logged in without a human."""


async def ensure_session(db: AsyncSession, realm: AuthRealm) -> dict:
    """Return the realm's auth state dict, logging in via scripted creds if needed.
    Raises NeedsLoginError when a human is required."""
    if realm.status == RealmStatus.ACTIVE and realm.state_snapshot:
        return realm.state_snapshot
    if realm.username and realm.password:
        await run_scripted_login(db, realm)
        return realm.state_snapshot
    await invalidate(db, realm, RealmStatus.NEEDS_LOGIN, "No stored session; assisted login required")
    raise NeedsLoginError(f"Realm {realm.id} needs an assisted login")


JIT_LOGIN_HORIZON = timedelta(hours=1)
# Within one firing, a run may log in again only once the session the firing's
# own (successful) login bought is dead or about to be — and not sooner than the
# floor after that login, so a session that reads as expired right away (odd
# cookie expiries) can't become one login per sibling run.
FIRING_RELOGIN_MARGIN = timedelta(minutes=5)
FIRING_RELOGIN_FLOOR = timedelta(minutes=30)


async def session_for_run(db: AsyncSession, realm: AuthRealm, *, run: ExtractionRun,
                          horizon: timedelta = JIT_LOGIN_HORIZON) -> dict:
    """Like ``ensure_session``, but a *job-triggered* run logs the realm in just in
    time when its session is missing or expires within ``horizon``.

    A scheduled run fires unattended, so a session that would lapse mid-run (or
    already has) is renewed up front instead of pausing at the first auth wall.
    Bounded on purpose: only runs with a ``job_run_id`` (manual/bulk/force runs
    behave exactly as ``ensure_session``), only realms with stored credentials,
    never a LOGIN_FAILED realm (retrying bad credentials on every firing risks
    an IdP lockout — a human must fix them), and normally once per realm per
    JobRun: a login stamped after the JobRun was created already serves every
    sibling run of that firing. The exception is a firing that outlives its own
    session (a Tier 1 firing runs ~9h; AvePoint's session lasts 2h): once the
    session that firing's last *successful* login bought has expired, a run
    logs in again (``_firing_session_spent``). A failed attempt still ends it
    for the firing."""
    if run.job_run_id is not None:
        if realm.status == RealmStatus.LOGIN_FAILED:
            raise NeedsLoginError(
                f"Realm {realm.id} scripted login failed earlier; fix its credentials")
        if realm.username and realm.password and _session_stale(realm, horizon):
            if (await _logged_in_this_firing(db, realm, run)
                    and not _firing_session_spent(realm)):
                # Already spent this firing's login (or its attempt — a
                # Browserless outage leaves the realm EXPIRED). Never defer to
                # ensure_session here: it logs in again for any non-ACTIVE
                # realm with credentials, which is the second login we refuse.
                if realm.status == RealmStatus.ACTIVE and realm.state_snapshot:
                    return realm.state_snapshot
                raise NeedsLoginError(
                    f"Realm {realm.id} already logged in once for this job firing")
            logger.info("Just-in-time login for realm %s (run %s)", realm.id, run.id)
            await run_scripted_login(db, realm)
            return realm.state_snapshot
    return await ensure_session(db, realm)


def _session_stale(realm: AuthRealm, horizon: timedelta) -> bool:
    expires = session_expires_at(realm)
    return (
        realm.status != RealmStatus.ACTIVE
        or not realm.state_snapshot
        or (expires is not None and expires <= datetime.now(timezone.utc) + horizon)
    )


def _firing_session_spent(realm: AuthRealm) -> bool:
    """Whether this firing's last login *succeeded* and the session it bought is
    now dead (or within ``FIRING_RELOGIN_MARGIN`` of it), at least
    ``FIRING_RELOGIN_FLOOR`` after that login. False after a failed attempt
    (``last_login_attempt_at`` newer than ``last_login_at``)."""
    if realm.status != RealmStatus.ACTIVE or realm.last_login_at is None:
        return False
    if (realm.last_login_attempt_at is not None
            and realm.last_login_attempt_at > realm.last_login_at):
        return False
    now = datetime.now(timezone.utc)
    if now - realm.last_login_at < FIRING_RELOGIN_FLOOR:
        return False
    expires = session_expires_at(realm)
    return expires is not None and expires <= now + FIRING_RELOGIN_MARGIN


async def _logged_in_this_firing(db: AsyncSession, realm: AuthRealm,
                                 run: ExtractionRun) -> bool:
    """Whether this JobRun already spent the realm's login: a login *attempt*
    (``last_login_attempt_at``, stamped whatever the outcome) or a success
    (``last_login_at``) after the JobRun was created."""
    stamps = [t for t in (realm.last_login_attempt_at, realm.last_login_at) if t is not None]
    if not stamps:
        return False
    job_run = await db.get(JobRun, run.job_run_id)
    if job_run is None or job_run.created_at is None:
        return False
    return max(stamps) >= job_run.created_at


def can_auto_relogin(realm: AuthRealm | None, run: ExtractionRun) -> bool:
    """Whether a mid-run auth wall may re-login a job run once (vs. pausing)."""
    return (
        realm is not None
        and run.job_run_id is not None
        and not run.auto_relogin_used
        and realm.status != RealmStatus.LOGIN_FAILED
        and bool(realm.username and realm.password)
    )


async def try_auto_relogin(db: AsyncSession, realm: AuthRealm | None,
                           run: ExtractionRun) -> bool:
    """Mid-run session expiry: re-login a job run's realm once.

    True when the scripted login succeeded (the caller re-queues the run). False
    when not eligible (see ``can_auto_relogin``) or the login failed — the realm
    is then LOGIN_FAILED (credentials rejected) or EXPIRED (Browserless
    unavailable) and the caller pauses as before. ``auto_relogin_used`` is
    set before trying, so a failed attempt is not retried on resume either."""
    if not can_auto_relogin(realm, run):
        return False
    run.auto_relogin_used = True
    try:
        await run_scripted_login(db, realm)
    except Exception as exc:
        logger.warning("Auto re-login for realm %s failed on run %s: %s",
                       realm.id, run.id, exc)
        return False
    logger.info("Session expired mid-run on %s: realm %s re-logged in", run.id, realm.id)
    return True


async def run_scripted_login(db: AsyncSession, realm: AuthRealm) -> None:
    """Run a headless scripted login; persist snapshot + ACTIVE, or LOGIN_FAILED.

    LOGIN_FAILED means *the credentials were rejected* (the login ran and did not
    complete) and locks the realm out of every future job firing until a human
    fixes them. An infrastructure failure (Browserless unreachable, 5xx,
    timeout) says nothing about the credentials, so it only marks the realm
    EXPIRED with the error — the next firing tries again — and still raises
    NeedsLoginError for this run. A realm already LOGIN_FAILED keeps it."""
    code, context = login_scripts.build_login(realm)
    # Record the attempt before making it: committed with whatever outcome
    # follows, it is what stops sibling runs of one firing logging in again.
    realm.last_login_attempt_at = datetime.now(timezone.utc)
    try:
        result = await browserless_client.run_login(code, context)
    except BrowserlessError as exc:
        status = (RealmStatus.LOGIN_FAILED if realm.status == RealmStatus.LOGIN_FAILED
                  else RealmStatus.EXPIRED)
        await invalidate(db, realm, status, f"Scripted login could not run: {exc}"[:4096])
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
