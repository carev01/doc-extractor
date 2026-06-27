"""AuthRealm model — credentials/session for a login-walled doc domain.

One realm per login domain (e.g. docs.cohesity.com). Secrets are encrypted at
rest. Runtime auth is carried by a named Browserless profile; state_snapshot is
a durable copy used to re-seed that profile if Browserless loses it.
"""

import uuid
from datetime import datetime
from enum import Enum

from sqlalchemy import DateTime, Enum as SAEnum, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.crypto import EncryptedJSON, EncryptedStr
from app.core.database import Base


class RealmStatus(str, Enum):
    ACTIVE = "active"
    NEEDS_LOGIN = "needs_login"
    EXPIRED = "expired"
    LOGIN_FAILED = "login_failed"


class AuthRealm(Base):
    __tablename__ = "auth_realms"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    login_domain: Mapped[str] = mapped_column(String(512), nullable=False)
    auth_type: Mapped[str] = mapped_column(String(32), default="form", nullable=False)
    login_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    login_selectors: Mapped[dict | None] = mapped_column(EncryptedJSON, nullable=True)

    username: Mapped[str | None] = mapped_column(EncryptedStr, nullable=True)
    password: Mapped[str | None] = mapped_column(EncryptedStr, nullable=True)
    totp_secret: Mapped[str | None] = mapped_column(EncryptedStr, nullable=True)

    browserless_profile_name: Mapped[str] = mapped_column(String(255), nullable=False)
    state_snapshot: Mapped[dict | None] = mapped_column(EncryptedJSON, nullable=True)

    status: Mapped[RealmStatus] = mapped_column(
        SAEnum(RealmStatus), default=RealmStatus.NEEDS_LOGIN, nullable=False
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(4096), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
