"""Authentication middleware — enforces auth on all /api/ routes.

Exempt paths (no auth required):
  - /api/health
  - /api/auth/login        (obviously)
  - /api/auth/refresh      (uses refresh token, not access token)
  - /api/auth/register     (creates the first user / new users)
  - /api/auth/oauth/*      (OAuth2 authorize + callback)
  - /api/extraction/webhook/*  (Firecrawl callbacks — can't authenticate)
  - /api/v1/webhooks/inbound   (external integrations with own signatures)

When auth is not configured (no JWT secret), the middleware is a no-op so
the server stays functional for local dev without auth.

Tests should monkeypatch ``app.core.auth_middleware._session_factory`` to
point at the test database session factory.
"""

import uuid
from datetime import datetime, timezone
from typing import Optional

import jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.core.config import settings
from app.core.database import async_session as _default_session_factory
from app.core.security import decode_token, hash_api_key
from app.models.api_key import APIKey
from app.models.user import User

# The session factory used by the middleware. Tests can override this by
# monkeypatching this module attribute.
_session_factory: async_sessionmaker = _default_session_factory

# Paths that bypass authentication entirely.
_EXEMPT_PREFIXES = (
    "/api/health",
    "/api/auth/login",
    "/api/auth/refresh",
    "/api/auth/register",
    "/api/auth/oauth/",
    "/api/extraction/webhook/",
    "/api/v1/webhooks/inbound",
)

# Non-API paths (docs, media, etc.) — no auth needed.
_NON_API_PREFIXES = (
    "/docs",
    "/redoc",
    "/openapi.json",
    "/media",
)


class AuthMiddleware(BaseHTTPMiddleware):
    """Validate Bearer tokens or API keys on /api/ routes."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Non-API paths pass through
        if path.startswith(_NON_API_PREFIXES) or not path.startswith("/api/"):
            return await call_next(request)

        # Exempt API paths pass through
        if path.startswith(_EXEMPT_PREFIXES):
            return await call_next(request)

        # If auth is not configured, allow through (dev mode)
        if not settings.auth_jwt_secret:
            return await call_next(request)

        # Try API key first (X-API-Key header)
        api_key_raw = request.headers.get("X-API-Key")
        if api_key_raw:
            user = await self._validate_api_key(api_key_raw)
            if user is None:
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Invalid API key"},
                )
            request.state.user = user
            return await call_next(request)

        # Then Bearer token
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            user = await self._validate_token(token)
            if user is None:
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Invalid or expired token"},
                )
            request.state.user = user
            return await call_next(request)

        # No credentials provided
        return JSONResponse(
            status_code=401,
            content={"detail": "Not authenticated"},
        )

    async def _validate_token(self, token: str) -> Optional[User]:
        try:
            payload = decode_token(token)
        except jwt.PyJWTError:
            return None

        if payload.get("type") != "access":
            return None

        user_id = payload.get("sub")
        if not user_id:
            return None

        async with _session_factory() as db:
            result = await db.execute(
                select(User).where(User.id == uuid.UUID(user_id))
            )
            user = result.scalar_one_or_none()
            if not user or not user.is_active:
                return None
            return user

    async def _validate_api_key(self, raw_key: str) -> Optional[User]:
        hashed = hash_api_key(raw_key)
        async with _session_factory() as db:
            result = await db.execute(
                select(APIKey).where(APIKey.hashed_key == hashed)
            )
            api_key = result.scalar_one_or_none()
            if not api_key or not api_key.is_active:
                return None
            if api_key.expires_at and api_key.expires_at < datetime.now(timezone.utc):
                return None
            if api_key.revoked_at is not None:
                return None

            result = await db.execute(
                select(User).where(User.id == api_key.user_id)
            )
            user = result.scalar_one_or_none()
            if not user or not user.is_active:
                return None

            api_key.last_used_at = datetime.now(timezone.utc)
            await db.commit()
            return user