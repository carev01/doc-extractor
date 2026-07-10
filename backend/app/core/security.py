"""Core authentication utilities — password hashing, JWT, API key hashing.

This module is deliberately framework-agnostic. FastAPI dependencies live in
``app.core.dependencies`` and call into these functions.
"""

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
import bcrypt
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.core.config import settings
from app.models.user import UserRole

# ---------------------------------------------------------------------------
# Password hashing — using bcrypt directly (passlib 1.7 is incompatible with
# bcrypt 5.x; calling bcrypt directly avoids the dependency conflict).
# ---------------------------------------------------------------------------

_BCRYPT_ROUNDS = 12


def hash_password(password: str) -> str:
    """Hash a password using bcrypt (12 rounds)."""
    # bcrypt truncates at 72 bytes; we pre-hash with SHA-256 to support any length.
    pre = hashlib.sha256(password.encode()).hexdigest()
    return bcrypt.hashpw(pre.encode(), bcrypt.gensalt(_BCRYPT_ROUNDS)).decode()


def verify_password(plain: str, hashed: str) -> bool:
    """Verify a password against a bcrypt hash."""
    pre = hashlib.sha256(plain.encode()).hexdigest()
    try:
        return bcrypt.checkpw(pre.encode(), hashed.encode())
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# API key generation / hashing
# ---------------------------------------------------------------------------

KEY_PREFIX = "dxk_"  # doc-extractor key
KEY_LENGTH = 32  # random bytes → ~43 base64url chars


def generate_api_key() -> tuple[str, str]:
    """Return (raw_key, hashed_key). The raw key is shown to the user once."""
    raw = KEY_PREFIX + secrets.token_urlsafe(KEY_LENGTH)
    return raw, hash_api_key(raw)


def hash_api_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def key_prefix_from_raw(raw: str) -> str:
    """Extract a display prefix for UI (first 12 chars including the prefix)."""
    return raw[:12]


# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------


def create_access_token(
    user_id: uuid.UUID,
    email: str,
    role: UserRole,
    expires_minutes: int | None = None,
) -> str:
    now = datetime.now(timezone.utc)
    exp = now + timedelta(minutes=expires_minutes or settings.auth_access_token_expire_minutes)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "email": email,
        "role": role.value,
        "type": "access",
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }
    return jwt.encode(payload, settings.auth_jwt_secret, algorithm=settings.auth_jwt_algorithm)


def create_refresh_token(user_id: uuid.UUID, expires_days: int | None = None) -> str:
    now = datetime.now(timezone.utc)
    exp = now + timedelta(days=expires_days or settings.auth_refresh_token_expire_days)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "type": "refresh",
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "jti": secrets.token_urlsafe(16),
    }
    return jwt.encode(payload, settings.auth_jwt_secret, algorithm=settings.auth_jwt_algorithm)


def decode_token(token: str) -> dict[str, Any]:
    """Decode a JWT. Raises jwt.PyJWTError on invalid/expired tokens."""
    return jwt.decode(
        token,
        settings.auth_jwt_secret,
        algorithms=[settings.auth_jwt_algorithm],
    )


# ---------------------------------------------------------------------------
# Role hierarchy
# ---------------------------------------------------------------------------

_ROLE_LEVEL: dict[UserRole, int] = {
    UserRole.READ_ONLY: 1,
    UserRole.READ_WRITE: 2,
    UserRole.ADMIN: 3,
}


def role_at_least(held: UserRole, required: UserRole) -> bool:
    return _ROLE_LEVEL[held] >= _ROLE_LEVEL[required]


def min_role(a: UserRole, b: UserRole) -> UserRole:
    """The more restrictive (lower) of two roles."""
    return a if _ROLE_LEVEL[a] <= _ROLE_LEVEL[b] else b


# ---------------------------------------------------------------------------
# OAuth2 `state` — signed + time-limited (CSRF protection, stateless)
# ---------------------------------------------------------------------------
#
# The state we hand the provider is a signed token binding the flow to a
# provider and a short TTL. On callback we verify the signature/TTL and that the
# provider matches — so a state we didn't issue (CSRF) or a stale one is
# rejected. Signed with the JWT secret, so it needs no server-side storage.

_OAUTH_STATE_SALT = "docextractor-oauth-state"
OAUTH_STATE_MAX_AGE_SECONDS = 600  # 10 minutes


def _state_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.auth_jwt_secret, salt=_OAUTH_STATE_SALT)


def make_oauth_state(provider: str) -> str:
    """Issue a signed, time-limited state token for an OAuth2 flow."""
    return _state_serializer().dumps({"provider": provider, "nonce": secrets.token_urlsafe(8)})


def verify_oauth_state(state: str, provider: str) -> bool:
    """Return True only for a state we issued for *provider*, within the TTL."""
    if not state:
        return False
    try:
        data = _state_serializer().loads(state, max_age=OAUTH_STATE_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired, Exception):  # noqa: BLE001
        return False
    return isinstance(data, dict) and data.get("provider") == provider