"""ExportJob model — queued export generation, drained by the worker."""

import uuid
from datetime import datetime
from enum import Enum

from sqlalchemy import DateTime, Enum as SAEnum, ForeignKey, Index, Integer, String, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ExportStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ExportJob(Base):
    __tablename__ = "export_jobs"

    __table_args__ = (
        Index("ix_export_jobs_pending", "created_at", postgresql_where=text("status = 'PENDING'")),
        Index("ix_export_jobs_batch", "batch_id", "batch_seq", postgresql_where=text("batch_id IS NOT NULL")),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documentation_sources.id", ondelete="CASCADE"), nullable=False
    )
    request: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[ExportStatus] = mapped_column(SAEnum(ExportStatus), default=ExportStatus.PENDING, nullable=False)

    claimed_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)

    # Product-level export: one ordinary per-source job per source, tied together
    # by a shared batch_id. NULL for an ordinary single-source export.
    batch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    batch_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Display name for the batch ("<Vendor> / <Product>"), denormalised so a batch
    # still reads correctly after its product is renamed or deleted.
    batch_label: Mapped[str | None] = mapped_column(String(512), nullable=True)

    export_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(4096), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
