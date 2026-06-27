"""Auth realm routes — manage login-walled doc credentials/sessions.

Revised design (Browserless OSS, no liveURL assisted login):
- CRUD (create, list, get, patch, delete)
- POST /{id}/login  — headless scripted login via stored credentials
- POST /{id}/session — upload a Playwright storageState snapshot
- POST /{id}/test   — verify the session reaches the doc domain
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.auth_realm import AuthRealm, RealmStatus
from app.schemas.auth_realm import (
    AuthRealmCreate,
    AuthRealmUpdate,
    AuthRealmResponse,
    SessionUpload,
)
from app.services.auth import realm_manager
from app.services.auth.realm_manager import NeedsLoginError
from app.services.browserless import browserless_client

router = APIRouter(prefix="/api/auth-realms", tags=["auth-realms"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize_origins(origins: list[dict]) -> list[dict]:
    """Normalise Playwright storageState localStorage [{name,value}] → [[k,v]].

    Playwright's ``context.storageState()`` emits localStorage entries as
    ``[{"name": k, "value": v}, ...]``.  Our wire format (and what the
    Browserless function module expects) is ``[[k, v], ...]``.  Already-
    normalised lists are left untouched.
    """
    result = []
    for origin in origins:
        ls = origin.get("localStorage", [])
        # Detect Playwright's {name, value} shape.
        if ls and isinstance(ls[0], dict) and "name" in ls[0]:
            ls = [[item["name"], item["value"]] for item in ls]
        result.append({**origin, "localStorage": ls})
    return result


def _response(realm: AuthRealm) -> AuthRealmResponse:
    """Build a safe response, projecting secrets to presence booleans only."""
    return AuthRealmResponse(
        id=realm.id,
        name=realm.name,
        login_domain=realm.login_domain,
        auth_type=realm.auth_type,
        login_url=realm.login_url,
        status=(
            realm.status.value if hasattr(realm.status, "value") else realm.status
        ),
        has_username=bool(realm.username),
        has_password=bool(realm.password),
        has_totp=bool(realm.totp_secret),
        last_login_at=realm.last_login_at,
        error_message=realm.error_message,
    )


async def _get_realm(db: AsyncSession, realm_id: uuid.UUID) -> AuthRealm:
    realm = await db.get(AuthRealm, realm_id)
    if realm is None:
        raise HTTPException(status_code=404, detail="Auth realm not found")
    return realm


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

@router.post("", status_code=201, response_model=AuthRealmResponse)
async def create_realm(payload: AuthRealmCreate, db: AsyncSession = Depends(get_db)):
    """Create a new auth realm for a login-walled documentation domain."""
    realm = AuthRealm(
        name=payload.name,
        login_domain=payload.login_domain,
        auth_type=payload.auth_type,
        login_url=payload.login_url,
        login_selectors=payload.login_selectors,
        username=payload.username,
        password=payload.password,
        totp_secret=payload.totp_secret,
        browserless_profile_name=f"realm-{uuid.uuid4()}",
        status=RealmStatus.NEEDS_LOGIN,
    )
    db.add(realm)
    await db.commit()
    await db.refresh(realm)
    return _response(realm)


@router.get("", response_model=list[AuthRealmResponse])
async def list_realms(db: AsyncSession = Depends(get_db)):
    """List all auth realms, ordered by name."""
    rows = (
        await db.execute(select(AuthRealm).order_by(AuthRealm.name))
    ).scalars().all()
    return [_response(r) for r in rows]


@router.get("/{realm_id}", response_model=AuthRealmResponse)
async def get_realm(realm_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Get a single auth realm by ID."""
    return _response(await _get_realm(db, realm_id))


@router.patch("/{realm_id}", response_model=AuthRealmResponse)
async def update_realm(
    realm_id: uuid.UUID,
    payload: AuthRealmUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Partially update an auth realm (name, credentials, selectors)."""
    realm = await _get_realm(db, realm_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(realm, field, value)
    await db.commit()
    await db.refresh(realm)
    return _response(realm)


@router.delete("/{realm_id}", status_code=204)
async def delete_realm(realm_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Delete an auth realm (sources referencing it become auth-less via SET NULL)."""
    realm = await _get_realm(db, realm_id)
    await db.delete(realm)
    await db.commit()


# ---------------------------------------------------------------------------
# Login actions
# ---------------------------------------------------------------------------

@router.post("/{realm_id}/login", response_model=AuthRealmResponse)
async def scripted_login(realm_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Trigger a headless scripted login using the realm's stored credentials.

    Requires ``username`` and ``password`` to be set on the realm.  Raises 400
    if credentials are missing (upload a session snapshot instead) and 409 if
    the login fails.
    """
    realm = await _get_realm(db, realm_id)
    if not (realm.username and realm.password):
        raise HTTPException(
            status_code=400,
            detail="Realm has no stored credentials; upload a session snapshot instead",
        )
    try:
        await realm_manager.run_scripted_login(db, realm)
    except NeedsLoginError as exc:
        await db.commit()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await db.commit()
    await db.refresh(realm)
    return _response(realm)


@router.post("/{realm_id}/session", response_model=AuthRealmResponse)
async def upload_session(
    realm_id: uuid.UUID,
    payload: SessionUpload,
    db: AsyncSession = Depends(get_db),
):
    """Upload a Playwright storageState snapshot to activate the realm.

    Accepts ``cookies`` and/or ``origins``.  Playwright's localStorage format
    (``[{name, value}]``) is normalised to ``[[k, v]]`` before storage.  At
    least one of ``cookies`` or ``origins`` must be non-empty; both empty
    returns 400.

    On success the realm ``state_snapshot`` is updated and ``status`` is set
    to ``ACTIVE``.
    """
    if not payload.cookies and not payload.origins:
        raise HTTPException(
            status_code=400,
            detail="At least one of 'cookies' or 'origins' must be non-empty",
        )
    realm = await _get_realm(db, realm_id)
    normalized_origins = _normalize_origins(payload.origins)
    realm.state_snapshot = {"cookies": payload.cookies, "origins": normalized_origins}
    realm.status = RealmStatus.ACTIVE
    realm.error_message = None
    await db.commit()
    await db.refresh(realm)
    return _response(realm)


@router.post("/{realm_id}/test", response_model=AuthRealmResponse)
async def test_realm(realm_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Fetch the realm's login_domain root through its session and verify it is
    not an auth wall.

    Uses ``ensure_session`` (will attempt scripted login if credentials are
    available and the snapshot is stale) then renders the domain root through
    Browserless with the auth state injected.  Sets status to EXPIRED if the
    rendered page is still an auth wall.
    """
    from app.services.blockpage import is_auth_wall

    realm = await _get_realm(db, realm_id)
    try:
        state = await realm_manager.ensure_session(db, realm)
    except NeedsLoginError as exc:
        await db.commit()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    html = await browserless_client.render_html(
        f"https://{realm.login_domain}/", auth_state=state
    )
    if is_auth_wall(html, final_url=f"https://{realm.login_domain}/",
                    login_domain=realm.login_domain):
        await realm_manager.invalidate(db, realm, RealmStatus.EXPIRED, "Test hit auth wall")
    await db.commit()
    await db.refresh(realm)
    return _response(realm)
