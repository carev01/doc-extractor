"""User model — local user identity for API authentication.

A user owns API keys and OAuth2 identities. Even OAuth2-authenticated users get
a local row so RBAC and key ownership work uniformly. Roles are per-user;
per-key roles can further restrict access but never exceed the user's own
permissions.
"""

import uuid
from datetime import datetime
from enum import Enum

from sqlalchemy import DateTime, Enum as SAEnum, String, Boolean, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class UserRole(str, Enum):
    """Role hierarchy: admin > read_write > read_only."""
    ADMIN = "admin"
    READ_WRITE = "read_write"
    READ_ONLY = "read_only"


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    hashed_password: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Store the lowercase role *values* ("admin"/…) as the PG enum labels — matches
    # the Alembic migration and the JSON representation. Without values_callable
    # SQLAlchemy would use the uppercase member *names*, which wouldn't match the
    # migration's enum type and would break inserts under `alembic upgrade head`.
    role: Mapped[UserRole] = mapped_column(
        SAEnum(UserRole, name="userrole", values_callable=lambda e: [m.value for m in e]),
        default=UserRole.READ_ONLY,
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # OAuth2 identity linkage — populated when the user first authenticates
    # through an external provider. ``oauth_provider`` is "google" | "okta" | None.
    oauth_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    oauth_subject: Mapped[str | None] = mapped_column(String(512), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )