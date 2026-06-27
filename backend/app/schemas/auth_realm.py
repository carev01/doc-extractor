"""AuthRealm request/response schemas. Secrets are write-only.

Responses expose only presence booleans (has_username, has_password, has_totp)
and never the raw credential values or state_snapshot.
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class AuthRealmCreate(BaseModel):
    name: str
    login_domain: str
    auth_type: Literal["form", "b2c", "oidc"] = "form"
    login_url: str | None = None
    login_selectors: dict | None = None
    username: str | None = None
    password: str | None = None
    totp_secret: str | None = None


class AuthRealmUpdate(BaseModel):
    name: str | None = None
    login_url: str | None = None
    login_selectors: dict | None = None
    username: str | None = None
    password: str | None = None
    totp_secret: str | None = None


class AuthRealmResponse(BaseModel):
    id: uuid.UUID
    name: str
    login_domain: str
    auth_type: str
    login_url: str | None
    status: str
    has_username: bool
    has_password: bool
    has_totp: bool
    last_login_at: datetime | None
    error_message: str | None


class SessionUpload(BaseModel):
    """Payload for the session-upload endpoint.

    Accepts a Playwright storageState snapshot: cookies and/or origins.
    localStorage under each origin may be either the Playwright-native
    [{name, value}] shape or the already-normalised [[k, v]] shape.
    """
    cookies: list[dict] = []
    origins: list[dict] = []  # [{origin, localStorage: [[k,v],...] OR [{name,value},...]}]
