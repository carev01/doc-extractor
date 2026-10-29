"""JobExclusion — a vendor a tier job must not cover through its tier.

Only the tier part of coverage is affected; explicit targets still win.
"""

import uuid

from sqlalchemy import ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class JobExclusion(Base):
    __tablename__ = "job_exclusions"
    __table_args__ = (
        UniqueConstraint("job_id", "vendor_id", name="uq_job_exclusions_job_vendor"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    vendor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vendors.id", ondelete="CASCADE"), nullable=False, index=True
    )

    job: Mapped["Job"] = relationship("Job", back_populates="exclusions")
