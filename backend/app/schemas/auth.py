"""Pydantic schemas for authentication endpoints."""

import uuid
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, EmailStr, Field

from app.models.user import UserRole


# ---------------------------------------------------------------------------
# User
# ---------------------------------------------------------------------------

class UserResponse(BaseModel):
    id: uuid.UUID
    email: EmailStr
    display_name: str
    role: UserRole
    is_active: bool
    oauth_provider: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class UserCreate(BaseModel):
    email: EmailStr
    display_name: str = Field(..., min_length=1, max_length=255)
    password: str = Field(..., min_length=8, max_length=128)
    role: UserRole = UserRole.READ_ONLY


# ---------------------------------------------------------------------------
# Token
# ---------------------------------------------------------------------------

class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds


class RefreshRequest(BaseModel):
    refresh_token: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


# ---------------------------------------------------------------------------
# API Key
# ---------------------------------------------------------------------------

class APIKeyCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    role: UserRole = UserRole.READ_ONLY
    expires_at: Optional[datetime] = None


class APIKeyResponse(BaseModel):
    id: uuid.UUID
    name: str
    key_prefix: str
    role: UserRole
    is_active: bool
    last_used_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    created_at: datetime
    revoked_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class APIKeyCreatedResponse(APIKeyResponse):
    """Returned only at creation time — includes the raw key once."""
    raw_key: str


# ---------------------------------------------------------------------------
# OAuth2
# ---------------------------------------------------------------------------

class OAuthAuthorizeResponse(BaseModel):
    authorization_url: str
    state: str


class OAuthCallbackRequest(BaseModel):
    code: str
    state: str


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

class AuthStatusResponse(BaseModel):
    """Public auth status — lets the frontend decide whether to show login."""
    auth_enabled: bool
    needs_bootstrap: bool


# ---------------------------------------------------------------------------
# User administration
# ---------------------------------------------------------------------------

class UserUpdate(BaseModel):
    display_name: Optional[str] = Field(None, min_length=1, max_length=255)
    role: Optional[UserRole] = None
    is_active: Optional[bool] = None


class UserListResponse(BaseModel):
    users: list[UserResponse]
    total: int


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(..., min_length=8, max_length=128)


# ---------------------------------------------------------------------------
# Per-vendor permissions
# ---------------------------------------------------------------------------

# Only the two positive access levels are assignable; "no access" = omit the vendor.
VENDOR_LEVELS = Literal["read_only", "read_write"]


class VendorPermissionItem(BaseModel):
    vendor_id: uuid.UUID
    level: VENDOR_LEVELS


class VendorPermissionSet(BaseModel):
    """Full replacement of a user's per-vendor grants."""
    permissions: list[VendorPermissionItem] = []


class VendorPermissionResponse(BaseModel):
    vendor_id: uuid.UUID
    vendor_name: str
    level: str


class VendorPermissionListResponse(BaseModel):
    user_id: uuid.UUID
    permissions: list[VendorPermissionResponse]


# ---------------------------------------------------------------------------
# Admin API-key oversight
# ---------------------------------------------------------------------------

class APIKeyAdminResponse(APIKeyResponse):
    """A key with its owner, for the admin cross-user key view."""
    user_id: uuid.UUID
    user_email: EmailStr