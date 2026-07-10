"""Authentication + authorization middleware — the single auth enforcement point.

For every ``/api/`` request (except the exempt paths below) this:
  1. resolves credentials (X-API-Key or Bearer) via the shared resolver in
     ``app.core.dependencies``,
  2. stashes the user + effective role on ``request.state`` (FastAPI deps read
     these; they do not re-validate), and
  3. enforces method-based RBAC: safe methods need read_only, mutating methods
     need read_write. Self-service ``/api/auth/*`` routes are authenticated but
     exempt from the method rule (a read_only user may manage their own keys).

Exempt paths (no auth at all):
  - /api/health
  - /api/auth/login | /register | /refresh | /status | /oauth/*
  - /api/extraction/webhook/*  (Firecrawl callbacks — keyed by an unguessable
    run_id; the caller can't authenticate)

When auth is not configured (no ``AUTH_JWT_SECRET``) the middleware is a no-op so
local dev works without auth.

Tests monkeypatch ``app.core.auth_middleware._session_factory`` to point at the
test database session factory.
"""

from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.core.config import settings
from app.core.database import async_session as _default_session_factory
from app.core.dependencies import resolve_user_from_credentials
from app.core.security import role_at_least
from app.models.user import UserRole

# Overridable by tests.
_session_factory: async_sessionmaker = _default_session_factory

# Paths that bypass authentication entirely.
_EXEMPT_PREFIXES = (
    "/api/health",
    "/api/auth/login",
    "/api/auth/register",
    "/api/auth/refresh",
    "/api/auth/status",
    "/api/auth/oauth/",
    "/api/extraction/webhook/",
)

# Non-API paths (docs, media, static) — no auth needed.
_NON_API_PREFIXES = ("/docs", "/redoc", "/openapi.json", "/media")

# Self-service auth routes: authenticated, but exempt from the method-based RBAC
# rule (a read_only user is allowed to POST to manage their own account/keys).
_SELF_SERVICE_PREFIX = "/api/auth/"

_SAFE_METHODS = frozenset({"GET", "HEAD"})


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # CORS preflight and non-API/static paths pass straight through.
        if request.method == "OPTIONS" or path.startswith(_NON_API_PREFIXES) or not path.startswith("/api/"):
            return await call_next(request)
        if path.startswith(_EXEMPT_PREFIXES):
            return await call_next(request)

        # Dev mode: auth disabled → open.
        if not settings.auth_jwt_secret:
            return await call_next(request)

        bearer = request.headers.get("Authorization", "")
        creds = await self._authenticate(
            bearer=bearer[7:].strip() if bearer.startswith("Bearer ") else None,
            api_key=request.headers.get("X-API-Key"),
        )
        if creds is None:
            return JSONResponse(status_code=401, content={"detail": "Not authenticated"})

        user, effective_role = creds
        request.state.user = user
        request.state.effective_role = effective_role

        # Method-based RBAC (skipped for self-service /api/auth/* routes, which
        # are self-scoped and guarded by their own handlers).
        if not path.startswith(_SELF_SERVICE_PREFIX):
            required = UserRole.READ_ONLY if request.method in _SAFE_METHODS else UserRole.READ_WRITE
            if not role_at_least(effective_role, required):
                return JSONResponse(
                    status_code=403,
                    content={"detail": f"Requires role: {required.value}"},
                )

        return await call_next(request)

    async def _authenticate(self, *, bearer, api_key):
        async with _session_factory() as db:
            return await resolve_user_from_credentials(db, bearer=bearer, api_key=api_key)
