"""Authentication routes — login, token refresh, API key CRUD, OAuth2 login.

OAuth2 password flow (email + password → JWT) and external OAuth2 (Google/Okta)
both produce local JWT access/refresh tokens. API keys are user-scoped and
managed through dedicated CRUD endpoints.

Registration is locked down: the very first user (bootstrap) is created as an
admin without auth; after that, only an admin may register users. OAuth2 `state`
is signed + verified to prevent login CSRF.
"""

import logging
import uuid
from datetime import datetime, timezone

import httpx
import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.dependencies import (
    authenticate_request,
    get_current_user,
    get_user_role,
)
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    generate_api_key,
    hash_password,
    key_prefix_from_raw,
    make_oauth_state,
    role_at_least,
    verify_oauth_state,
    verify_password,
)
from app.models.api_key import APIKey
from app.models.user import User, UserRole
from app.schemas.auth import (
    APIKeyCreate,
    APIKeyCreatedResponse,
    APIKeyResponse,
    AuthStatusResponse,
    LoginRequest,
    OAuthAuthorizeResponse,
    RefreshRequest,
    TokenResponse,
    UserCreate,
    UserResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

_OAUTH_PROVIDERS = {
    "google": {
        "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "userinfo_url": "https://www.googleapis.com/oauth2/v3/userinfo",
        "scope": "openid email profile",
    },
    "okta": {"scope": "openid email profile"},
}


def _auth_disabled() -> None:
    if not settings.auth_jwt_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication is not configured on this server",
        )


def _tokens_for_user(user: User) -> TokenResponse:
    return TokenResponse(
        access_token=create_access_token(user.id, user.email, user.role),
        refresh_token=create_refresh_token(user.id),
        expires_in=settings.auth_access_token_expire_minutes * 60,
    )


async def _user_count(db: AsyncSession) -> int:
    return (await db.execute(select(func.count(User.id)))).scalar() or 0


# ---------------------------------------------------------------------------
# Status (public — lets the frontend decide whether to show a login screen)
# ---------------------------------------------------------------------------

@router.get("/status", response_model=AuthStatusResponse)
async def auth_status(db: AsyncSession = Depends(get_db)):
    enabled = bool(settings.auth_jwt_secret)
    needs_bootstrap = enabled and (await _user_count(db)) == 0
    return AuthStatusResponse(auth_enabled=enabled, needs_bootstrap=needs_bootstrap)


# ---------------------------------------------------------------------------
# User registration — bootstrap first admin, then admin-only
# ---------------------------------------------------------------------------

@router.post("/register", response_model=UserResponse, status_code=201)
async def register_user(
    body: UserCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Register a user.

    - **Bootstrap**: when no users exist, the first registration is allowed
      without authentication and is forced to the ``admin`` role (ignoring any
      requested role) — there is no admin yet to authorize it.
    - **Afterwards**: registration requires an authenticated **admin**, who
      chooses the new user's role. Self-service admin sign-up is not possible.
    """
    _auth_disabled()

    if await _user_count(db) == 0:
        role = UserRole.ADMIN  # bootstrap admin; requested role is ignored
    else:
        creds = await authenticate_request(request, db)
        if creds is None or not role_at_least(creds[1], UserRole.ADMIN):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only an administrator can register new users",
            )
        role = body.role

    existing = await db.execute(select(User).where(User.email == body.email))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Email already registered")

    user = User(
        email=body.email,
        display_name=body.display_name,
        hashed_password=hash_password(body.password),
        role=role,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


# ---------------------------------------------------------------------------
# Login / refresh / me
# ---------------------------------------------------------------------------

@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, db: AsyncSession = Depends(get_db)):
    """Authenticate with email + password, receive JWT access + refresh tokens."""
    _auth_disabled()
    result = await db.execute(select(User).where(User.email == body.email))
    user = result.scalar_one_or_none()
    # Uniform 401 whether the user is missing or the password is wrong (no
    # account enumeration).
    if not user or not user.hashed_password or not verify_password(body.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is disabled")
    return _tokens_for_user(user)


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(body: RefreshRequest, db: AsyncSession = Depends(get_db)):
    """Exchange a refresh token for a new access + refresh token pair."""
    _auth_disabled()
    try:
        payload = decode_token(body.refresh_token)
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    if payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Invalid token type")
    sub = payload.get("sub")
    try:
        user_id = uuid.UUID(str(sub))
    except (ValueError, TypeError):
        raise HTTPException(status_code=401, detail="Invalid token")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    return _tokens_for_user(user)


@router.get("/me", response_model=UserResponse)
async def get_me(user: User = Depends(get_current_user)):
    return user


# ---------------------------------------------------------------------------
# API Key CRUD (self-service — any authenticated user manages their own keys)
# ---------------------------------------------------------------------------

@router.post("/keys", response_model=APIKeyCreatedResponse, status_code=201)
async def create_api_key(
    body: APIKeyCreate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Generate a new API key. The raw key is shown only in this response.

    A key can never grant more than the caller's own effective role (so a
    restricted key/session cannot mint a more powerful one)."""
    _auth_disabled()

    caller_role = get_user_role(request)
    if not role_at_least(caller_role, body.role):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"API key role '{body.role.value}' exceeds your role '{caller_role.value}'",
        )

    raw_key, hashed = generate_api_key()
    api_key = APIKey(
        user_id=user.id,
        name=body.name,
        key_prefix=key_prefix_from_raw(raw_key),
        hashed_key=hashed,
        role=body.role,
        expires_at=body.expires_at,
    )
    db.add(api_key)
    await db.commit()
    await db.refresh(api_key)

    return APIKeyCreatedResponse(
        id=api_key.id,
        name=api_key.name,
        key_prefix=api_key.key_prefix,
        role=api_key.role,
        is_active=api_key.is_active,
        last_used_at=api_key.last_used_at,
        expires_at=api_key.expires_at,
        created_at=api_key.created_at,
        revoked_at=api_key.revoked_at,
        raw_key=raw_key,
    )


@router.get("/keys", response_model=list[APIKeyResponse])
async def list_api_keys(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List the current user's API keys (never reveals key values)."""
    result = await db.execute(
        select(APIKey).where(APIKey.user_id == user.id).order_by(APIKey.created_at.desc())
    )
    return result.scalars().all()


@router.delete("/keys/{key_id}", status_code=204)
async def revoke_api_key(
    key_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Revoke one of the caller's API keys."""
    result = await db.execute(
        select(APIKey).where(APIKey.id == key_id, APIKey.user_id == user.id)
    )
    api_key = result.scalar_one_or_none()
    if not api_key:
        raise HTTPException(status_code=404, detail="API key not found")
    api_key.is_active = False
    api_key.revoked_at = datetime.now(timezone.utc)
    await db.commit()


# ---------------------------------------------------------------------------
# OAuth2 external providers (Google, Okta)
# ---------------------------------------------------------------------------

def _build_redirect_uri(provider: str) -> str:
    return f"{settings.auth_oauth_redirect_base.rstrip('/')}/api/auth/oauth/{provider}/callback"


def _get_provider_config(provider: str) -> dict:
    if provider == "google":
        if not settings.auth_google_client_id:
            raise HTTPException(status_code=400, detail="Google OAuth2 is not configured")
        return {
            **_OAUTH_PROVIDERS["google"],
            "client_id": settings.auth_google_client_id,
            "client_secret": settings.auth_google_client_secret,
        }
    if provider == "okta":
        if not settings.auth_okta_client_id or not settings.auth_okta_domain:
            raise HTTPException(status_code=400, detail="Okta OAuth2 is not configured")
        domain = settings.auth_okta_domain.rstrip("/")
        return {
            "authorize_url": f"{domain}/oauth2/default/v1/authorize",
            "token_url": f"{domain}/oauth2/default/v1/token",
            "userinfo_url": f"{domain}/oauth2/default/v1/userinfo",
            "scope": "openid email profile",
            "client_id": settings.auth_okta_client_id,
            "client_secret": settings.auth_okta_client_secret,
        }
    raise HTTPException(status_code=400, detail=f"Unknown provider: {provider}")


@router.get("/oauth/{provider}/authorize", response_model=OAuthAuthorizeResponse)
async def oauth_authorize(provider: str):
    """Return the authorization URL for the external OAuth2 provider, with a
    signed `state` the callback verifies (CSRF protection)."""
    _auth_disabled()
    cfg = _get_provider_config(provider)
    state = make_oauth_state(provider)
    params = {
        "response_type": "code",
        "client_id": cfg["client_id"],
        "redirect_uri": _build_redirect_uri(provider),
        "scope": cfg["scope"],
        "state": state,
    }
    from urllib.parse import urlencode
    url = f"{cfg['authorize_url']}?{urlencode(params)}"
    return OAuthAuthorizeResponse(authorization_url=url, state=state)


@router.get("/oauth/{provider}/callback", response_model=TokenResponse)
async def oauth_callback(
    provider: str,
    code: str,
    state: str,
    db: AsyncSession = Depends(get_db),
):
    """Handle the OAuth2 callback — verify state, exchange code, upsert user."""
    _auth_disabled()
    cfg = _get_provider_config(provider)

    # CSRF: reject any state we didn't issue for this provider (or that expired).
    if not verify_oauth_state(state, provider):
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state")

    async with httpx.AsyncClient(timeout=15) as client:
        token_resp = await client.post(
            cfg["token_url"],
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": _build_redirect_uri(provider),
                "client_id": cfg["client_id"],
                "client_secret": cfg["client_secret"],
            },
            headers={"Accept": "application/json"},
        )
        if token_resp.status_code != 200:
            logger.warning("OAuth token exchange failed for %s: %s", provider, token_resp.text)
            raise HTTPException(status_code=400, detail="OAuth token exchange failed")
        token_data = token_resp.json()

        access = token_data.get("access_token")
        if not access:
            raise HTTPException(status_code=400, detail="OAuth provider returned no access token")

        userinfo_resp = await client.get(
            cfg["userinfo_url"], headers={"Authorization": f"Bearer {access}"}
        )
        if userinfo_resp.status_code != 200:
            raise HTTPException(status_code=400, detail="Failed to fetch user info")
        userinfo = userinfo_resp.json()

    email = userinfo.get("email")
    if not email:
        raise HTTPException(status_code=400, detail="Provider did not return an email")
    provider_subject = userinfo.get("sub", "")
    display_name = userinfo.get("name", email)

    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if not user:
        # New external users get the least-privileged role; an admin can elevate.
        user = User(
            email=email,
            display_name=display_name,
            role=UserRole.READ_ONLY,
            oauth_provider=provider,
            oauth_subject=provider_subject,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
    elif user.oauth_provider != provider or user.oauth_subject != provider_subject:
        user.oauth_provider = provider
        user.oauth_subject = provider_subject
        await db.commit()

    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is disabled")
    return _tokens_for_user(user)
