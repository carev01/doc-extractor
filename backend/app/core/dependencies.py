"""FastAPI dependencies for authentication and authorization.

Two authentication paths:
1. **Bearer JWT** — OAuth2 password flow or OAuth2 external login → access token
2. **API Key** — ``X-API-Key`` header → hashed key lookup

Both resolve to a ``User`` row that downstream handlers use for RBAC checks.
"""

import uuid
from datetime import datetime, timezone
from typing import Optional

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import APIKeyHeader, OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.security import decode_token, hash_api_key, role_at_least, verify_password
from app.models.api_key import APIKey
from app.models.user import User, UserRole

# OAuth2 password bearer — tokenUrl points to our login endpoint for Swagger UI
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def _resolve_jwt_user(
    token: str,
    db: AsyncSession,
) -> User:
    try:
        payload = decode_token(token)
    except jwt.PyJWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if payload.get("type") != "access":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token type",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    result = await db.execute(select(User).where(User.id == uuid.UUID(user_id)))
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
        )
    return user


async def _resolve_api_key_user(
    raw_key: str,
    db: AsyncSession,
) -> User:
    hashed = hash_api_key(raw_key)
    result = await db.execute(select(APIKey).where(APIKey.hashed_key == hashed))
    api_key = result.scalar_one_or_none()
    if not api_key or not api_key.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )
    if api_key.expires_at and api_key.expires_at < datetime.now(timezone.utc):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key expired",
        )
    if api_key.revoked_at is not None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key revoked",
        )

    # Load the owning user
    result = await db.execute(select(User).where(User.id == api_key.user_id))
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key owner is inactive",
        )

    # Update last_used_at (don't block on failure)
    api_key.last_used_at = datetime.now(timezone.utc)
    await db.flush()

    # The effective role is the more restrictive of the key's role and the user's role
    effective_role = api_key.role if role_at_least(user.role, api_key.role) else user.role
    # Attach the effective role so downstream checks can use it
    user._effective_role = effective_role  # type: ignore[attr-defined]
    return user


async def get_current_user(
    token: Optional[str] = Depends(oauth2_scheme),
    api_key: Optional[str] = Depends(api_key_header),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Resolve the authenticated user from either a Bearer token or API key.

    Raises 401 if no valid credential is provided. When auth is disabled
    (no JWT secret configured), raises 503.
    """
    if not settings.auth_jwt_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication is not configured on this server",
        )

    if api_key:
        return await _resolve_api_key_user(api_key, db)

    if token:
        return await _resolve_jwt_user(token, db)

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
        headers={"WWW-Authenticate": 'Bearer realm="docextractor"'},
    )


def get_user_role(user: User) -> UserRole:
    """Return the effective role (API keys may restrict it)."""
    return getattr(user, "_effective_role", None) or user.role


def require_roles(*roles: UserRole):
    """Dependency factory: require the authenticated user to hold one of the roles.

    The user's effective role (which may be restricted by an API key) is checked.
    Admins implicitly satisfy any role requirement.
    """

    async def _check(user: User = Depends(get_current_user)) -> User:
        held = get_user_role(user)
        for r in roles:
            if role_at_least(held, r):
                return user
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Requires role: {', '.join(r.value for r in roles)}",
        )

    return _check


# Convenience constants
require_admin = require_roles(UserRole.ADMIN)
require_read_write = require_roles(UserRole.READ_WRITE, UserRole.ADMIN)
require_read_only = require_roles(UserRole.READ_ONLY, UserRole.READ_WRITE, UserRole.ADMIN)