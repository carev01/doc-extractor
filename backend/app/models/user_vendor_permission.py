"""Per-vendor access grants (row-level authorization).

Allow-list model: a non-admin user can access a vendor's data ONLY if a grant
row exists here; absence of a row means the vendor is invisible to them. The
grant's ``level`` sets read-only vs read-write for that vendor (capped by the
user's global role). Admins bypass this table entirely.
"""

import uuid
from datetime import datetime
from enum import Enum

from sqlalchemy import (
    DateTime, Enum as SAEnum, ForeignKey, UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class VendorAccessLevel(str, Enum):
    """Access level a per-vendor grant confers. 'No access' is the absence of a
    grant, so only the two positive levels exist here."""
    READ_ONLY = "read_only"
    READ_WRITE = "read_write"


class UserVendorPermission(Base):
    __tablename__ = "user_vendor_permissions"

    __table_args__ = (
        UniqueConstraint("user_id", "vendor_id", name="uq_user_vendor"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    vendor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("vendors.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Store the lowercase values as the PG enum labels (see models/user.py).
    level: Mapped[VendorAccessLevel] = mapped_column(
        SAEnum(VendorAccessLevel, name="vendoraccesslevel",
               values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    user: Mapped["User"] = relationship("User", backref="vendor_permissions")  # noqa: F821
    vendor: Mapped["Vendor"] = relationship("Vendor")  # noqa: F821
