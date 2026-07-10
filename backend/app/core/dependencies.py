"""Authentication & authorization — shared credential resolution + FastAPI deps.

There is a single authentication path. ``AuthMiddleware`` (app.core.auth_middleware)
resolves credentials once per request via ``resolve_user_from_credentials`` here,
stashes the authenticated user and its *effective* role on ``request.state``, and
enforces method-based RBAC. The FastAPI dependencies below simply read that state
— they do not re-validate — so auth logic lives in exactly one place.

Effective role: a JWT carries the user's own role; an API key's effective role is
the more restrictive of the key's role and the owning user's role (a key can
narrow, never widen, its owner's access).
"""

import uuid
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import decode_token, hash_api_key, min_role, role_at_least
from app.models.api_key import APIKey
from app.models.user import User, UserRole

# Only rewrite an API key's last_used_at at most this often, to avoid a DB write
# on every single authenticated request.
_LAST_USED_THROTTLE = timedelta(seconds=60)


async def _resolve_jwt(token: str, db: AsyncSession) -> "tuple[User, UserRole] | None":
    try:
        payload = decode_token(token)
    except jwt.PyJWTError:
        return None
    if payload.get("type") != "access":
        return None
    sub = payload.get("sub")
    if not sub:
        return None
    try:
        user_id = uuid.UUID(str(sub))
    except (ValueError, TypeError):
        return None
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not user or not user.is_active:
        return None
    return user, user.role


async def _resolve_api_key(raw_key: str, db: AsyncSession) -> "tuple[User, UserRole] | None":
    hashed = hash_api_key(raw_key)
    api_key = (
        await db.execute(select(APIKey).where(APIKey.hashed_key == hashed))
    ).scalar_one_or_none()
    if not api_key or not api_key.is_active or api_key.revoked_at is not None:
        return None
    now = datetime.now(timezone.utc)
    if api_key.expires_at and api_key.expires_at < now:
        return None
    user = (
        await db.execute(select(User).where(User.id == api_key.user_id))
    ).scalar_one_or_none()
    if not user or not user.is_active:
        return None
    # Throttled last-used bookkeeping (avoid a write on every request).
    if api_key.last_used_at is None or api_key.last_used_at < now - _LAST_USED_THROTTLE:
        api_key.last_used_at = now
        await db.commit()
    # A key narrows but never widens its owner's role.
    return user, min_role(user.role, api_key.role)


async def resolve_user_from_credentials(
    db: AsyncSession,
    *,
    bearer: str | None = None,
    api_key: str | None = None,
) -> "tuple[User, UserRole] | None":
    """Authenticate a request's credentials → (user, effective_role) or None.

    API key takes precedence over a Bearer token when both are present. Never
    raises — callers decide how to respond to ``None``.
    """
    if api_key:
        return await _resolve_api_key(api_key, db)
    if bearer:
        return await _resolve_jwt(bearer, db)
    return None


def _bearer_from_header(value: str | None) -> str | None:
    if value and value.startswith("Bearer "):
        return value[7:].strip() or None
    return None


async def authenticate_request(request: Request, db: AsyncSession) -> "tuple[User, UserRole] | None":
    """Resolve credentials straight off a Request (for exempt routes that need
    to conditionally check the caller, e.g. registration)."""
    return await resolve_user_from_credentials(
        db,
        bearer=_bearer_from_header(request.headers.get("Authorization")),
        api_key=request.headers.get("X-API-Key"),
    )


# ── FastAPI dependencies (read state set by AuthMiddleware) ─────────────────

async def get_current_user(request: Request) -> User:
    """Return the user AuthMiddleware authenticated for this request.

    503 when auth is not configured; 401 when no valid credential was supplied.
    """
    if not settings.auth_jwt_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication is not configured on this server",
        )
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": 'Bearer realm="docextractor"'},
        )
    return user


def get_user_role(request: Request) -> UserRole:
    """The effective role AuthMiddleware resolved for this request."""
    role = getattr(request.state, "effective_role", None)
    if role is not None:
        return role
    user = getattr(request.state, "user", None)
    return user.role if user is not None else UserRole.READ_ONLY


def require_roles(*required: UserRole):
    """Dependency factory: require the effective role to satisfy one of *required*
    (admins satisfy everything). Enforcement mirrors the middleware; use on a
    route only for finer control than the method-based default."""

    async def _check(request: Request, user: User = Depends(get_current_user)) -> User:
        held = get_user_role(request)
        if any(role_at_least(held, r) for r in required):
            return user
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Requires role: {', '.join(r.value for r in required)}",
        )

    return _check


require_admin = require_roles(UserRole.ADMIN)
require_read_write = require_roles(UserRole.READ_WRITE, UserRole.ADMIN)
require_read_only = require_roles(UserRole.READ_ONLY, UserRole.READ_WRITE, UserRole.ADMIN)
