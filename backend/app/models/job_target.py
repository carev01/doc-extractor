"""JobTarget — one explicit vendor / product / source a job covers."""

import uuid

from sqlalchemy import CheckConstraint, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class JobTarget(Base):
    __tablename__ = "job_targets"
    __table_args__ = (
        CheckConstraint(
            "num_nonnulls(vendor_id, product_id, source_id) = 1",
            name="ck_job_targets_exactly_one_ref",
        ),
        UniqueConstraint("job_id", "vendor_id", name="uq_job_targets_job_vendor"),
        UniqueConstraint("job_id", "product_id", name="uq_job_targets_job_product"),
        UniqueConstraint("job_id", "source_id", name="uq_job_targets_job_source"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vendors.id", ondelete="CASCADE"), nullable=True, index=True
    )
    product_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), nullable=True, index=True
    )
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documentation_sources.id", ondelete="CASCADE"),
        nullable=True, index=True,
    )

    job: Mapped["Job"] = relationship("Job", back_populates="targets")
