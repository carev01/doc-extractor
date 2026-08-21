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
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authz import Principal, get_principal
from app.core.config import settings
from app.core.database import get_db
from app.core.dependencies import (
    authenticate_request,
    get_current_user,
    get_user_role,
    require_admin,
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
from app.models.user_vendor_permission import UserVendorPermission, VendorAccessLevel
from app.models.vendor import Vendor
from app.schemas.auth import (
    APIKeyAdminResponse,
    APIKeyCreate,
    APIKeyCreatedResponse,
    APIKeyResponse,
    AuthStatusResponse,
    ChangePasswordRequest,
    LoginRequest,
    MyAccessResponse,
    OAuthAuthorizeResponse,
    RefreshRequest,
    TokenResponse,
    UserCreate,
    UserListResponse,
    UserResponse,
    UserUpdate,
    VendorPermissionItem,
    VendorPermissionListResponse,
    VendorPermissionResponse,
    VendorPermissionSet,
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


@router.get("/my-access", response_model=MyAccessResponse)
async def get_my_access(principal: Principal = Depends(get_principal)):
    """The caller's own effective access (see_all + role + per-vendor grants).

    Exists so the UI can hide controls the caller can't use rather than render
    buttons that 403 on click. Read-only by construction: it reports the same
    Principal the enforcement helpers already build, so it cannot widen access
    and cannot drift from what the routes actually allow. Not a substitute for
    enforcement — every mutating route still authorizes independently.
    """
    return MyAccessResponse(
        see_all=principal.see_all,
        role=principal.role.value,
        vendors=[
            VendorPermissionItem(vendor_id=vid, level=lvl.value)
            for vid, lvl in principal.vendor_levels.items()
        ],
    )


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
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Revoke an API key. Users may revoke their own; admins may revoke any."""
    api_key = (await db.execute(select(APIKey).where(APIKey.id == key_id))).scalar_one_or_none()
    is_admin = get_user_role(request) == UserRole.ADMIN
    if not api_key or (api_key.user_id != user.id and not is_admin):
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


# ---------------------------------------------------------------------------
# Self-service: change password + rotate own key
# ---------------------------------------------------------------------------

@router.post("/change-password", status_code=204)
async def change_password(
    body: ChangePasswordRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Change the current user's password (verifies the current one first)."""
    _auth_disabled()
    # `user` comes from the middleware's (closed) session, so re-load it into this
    # request's session before mutating — otherwise the commit would be a no-op.
    target = await db.get(User, user.id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    if not target.hashed_password:
        raise HTTPException(
            status_code=400,
            detail="This account signs in via an external provider and has no password",
        )
    if not verify_password(body.current_password, target.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Current password is incorrect")
    target.hashed_password = hash_password(body.new_password)
    await db.commit()


async def _rotate(db: AsyncSession, api_key: APIKey) -> APIKeyCreatedResponse:
    """Issue a replacement key (same name/role/expiry), revoke the old one."""
    raw_key, hashed = generate_api_key()
    new_key = APIKey(
        user_id=api_key.user_id,
        name=api_key.name,
        key_prefix=key_prefix_from_raw(raw_key),
        hashed_key=hashed,
        role=api_key.role,
        expires_at=api_key.expires_at,
    )
    api_key.is_active = False
    api_key.revoked_at = datetime.now(timezone.utc)
    db.add(new_key)
    await db.commit()
    await db.refresh(new_key)
    return APIKeyCreatedResponse(
        id=new_key.id, name=new_key.name, key_prefix=new_key.key_prefix,
        role=new_key.role, is_active=new_key.is_active, last_used_at=new_key.last_used_at,
        expires_at=new_key.expires_at, created_at=new_key.created_at,
        revoked_at=new_key.revoked_at, raw_key=raw_key,
    )


@router.post("/keys/{key_id}/rotate", response_model=APIKeyCreatedResponse, status_code=201)
async def rotate_api_key(
    key_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Rotate a key: mint a replacement and revoke the old one (raw shown once).
    Users may rotate their own keys; admins may rotate any key."""
    _auth_disabled()
    api_key = (await db.execute(select(APIKey).where(APIKey.id == key_id))).scalar_one_or_none()
    is_admin = get_user_role(request) == UserRole.ADMIN
    if not api_key or (api_key.user_id != user.id and not is_admin):
        raise HTTPException(status_code=404, detail="API key not found")
    return await _rotate(db, api_key)


# ---------------------------------------------------------------------------
# Admin: cross-user API-key oversight
# ---------------------------------------------------------------------------

@router.get("/admin/keys", response_model=list[APIKeyAdminResponse])
async def admin_list_keys(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """List every API key with its owner (admin oversight)."""
    rows = (
        await db.execute(
            select(APIKey, User.email)
            .join(User, User.id == APIKey.user_id)
            .order_by(APIKey.created_at.desc())
        )
    ).all()
    return [
        APIKeyAdminResponse(
            id=k.id, name=k.name, key_prefix=k.key_prefix, role=k.role,
            is_active=k.is_active, last_used_at=k.last_used_at, expires_at=k.expires_at,
            created_at=k.created_at, revoked_at=k.revoked_at,
            user_id=k.user_id, user_email=email,
        )
        for k, email in rows
    ]


# ---------------------------------------------------------------------------
# Admin: user management
# ---------------------------------------------------------------------------

async def _count_active_admins(db: AsyncSession) -> int:
    return (
        await db.execute(
            select(func.count(User.id)).where(User.role == UserRole.ADMIN, User.is_active.is_(True))
        )
    ).scalar() or 0


@router.get("/users", response_model=UserListResponse)
async def list_users(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    users = (await db.execute(select(User).order_by(User.created_at.asc()))).scalars().all()
    return UserListResponse(users=list(users), total=len(users))


@router.patch("/users/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: uuid.UUID,
    body: UserUpdate,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Update a user's display name, role, or active state (admin only).

    Guards against locking everyone out: an admin cannot demote or deactivate
    their own account, nor remove the last remaining active admin."""
    target = await db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    demoting = body.role is not None and body.role != UserRole.ADMIN and target.role == UserRole.ADMIN
    deactivating = body.is_active is False and target.is_active

    if target.id == admin.id and (demoting or deactivating):
        raise HTTPException(status_code=400, detail="You cannot demote or deactivate your own account")
    if (demoting or deactivating) and target.role == UserRole.ADMIN and await _count_active_admins(db) <= 1:
        raise HTTPException(status_code=400, detail="Cannot remove the last active admin")

    if body.display_name is not None:
        target.display_name = body.display_name
    if body.role is not None:
        target.role = body.role
    if body.is_active is not None:
        target.is_active = body.is_active
    await db.commit()
    await db.refresh(target)
    return target


@router.delete("/users/{user_id}", status_code=204)
async def delete_user(
    user_id: uuid.UUID,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Delete a user and their keys + vendor grants (admin only)."""
    target = await db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if target.id == admin.id:
        raise HTTPException(status_code=400, detail="You cannot delete your own account")
    if target.role == UserRole.ADMIN and await _count_active_admins(db) <= 1:
        raise HTTPException(status_code=400, detail="Cannot delete the last active admin")
    # Delete children explicitly: the api_keys/vendor_permissions relationships are
    # backrefs without an ORM delete-cascade, so a plain db.delete(user) would try
    # to NULL their (NOT NULL) user_id. The DB FKs are ON DELETE CASCADE, but the
    # ORM manages the relationship first — so remove children ourselves.
    await db.execute(delete(APIKey).where(APIKey.user_id == target.id))
    await db.execute(delete(UserVendorPermission).where(UserVendorPermission.user_id == target.id))
    await db.delete(target)
    await db.commit()


# ---------------------------------------------------------------------------
# Admin: per-vendor permissions
# ---------------------------------------------------------------------------

@router.get("/users/{user_id}/vendor-permissions", response_model=VendorPermissionListResponse)
async def get_vendor_permissions(
    user_id: uuid.UUID,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    if not await db.get(User, user_id):
        raise HTTPException(status_code=404, detail="User not found")
    rows = (
        await db.execute(
            select(UserVendorPermission.vendor_id, Vendor.name, UserVendorPermission.level)
            .join(Vendor, Vendor.id == UserVendorPermission.vendor_id)
            .where(UserVendorPermission.user_id == user_id)
            .order_by(Vendor.name.asc())
        )
    ).all()
    return VendorPermissionListResponse(
        user_id=user_id,
        permissions=[
            VendorPermissionResponse(vendor_id=vid, vendor_name=name, level=level.value)
            for vid, name, level in rows
        ],
    )


@router.put("/users/{user_id}/vendor-permissions", response_model=VendorPermissionListResponse)
async def set_vendor_permissions(
    user_id: uuid.UUID,
    body: VendorPermissionSet,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Replace a user's per-vendor grants wholesale (admin only).

    Each grant's level is capped by the user's global role (a global read_only
    user cannot be granted read_write on any vendor). Vendors omitted here become
    invisible to the user."""
    target = await db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    # Validate vendors exist and levels don't exceed the user's global role.
    ceiling_rw = role_at_least(target.role, UserRole.READ_WRITE)
    seen: set[uuid.UUID] = set()
    for item in body.permissions:
        if item.vendor_id in seen:
            raise HTTPException(status_code=400, detail=f"Duplicate vendor {item.vendor_id}")
        seen.add(item.vendor_id)
        if not await db.get(Vendor, item.vendor_id):
            raise HTTPException(status_code=400, detail=f"Unknown vendor {item.vendor_id}")
        if item.level == "read_write" and not ceiling_rw:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot grant read_write on {item.vendor_id}: user's global role is read_only",
            )

    # Replace all grants for this user. The delete is issued as a statement and
    # flushed *before* the inserts: SQLAlchemy's unit of work orders INSERTs
    # ahead of DELETEs within a flush, so ORM-deleting the old rows and adding
    # the new ones together made any re-grant of an already-granted vendor
    # collide with uq_user_vendor (user_id, vendor_id). The first save for a user
    # worked — there was nothing to delete — and every later edit failed.
    await db.execute(
        delete(UserVendorPermission).where(UserVendorPermission.user_id == user_id)
    )
    await db.flush()
    for item in body.permissions:
        db.add(UserVendorPermission(
            user_id=user_id, vendor_id=item.vendor_id,
            level=VendorAccessLevel(item.level),
        ))
    await db.commit()
    return await get_vendor_permissions(user_id, admin=target, db=db)
