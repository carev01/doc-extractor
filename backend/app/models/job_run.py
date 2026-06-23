"""JobRun model — one execution of a Job, fanned out into per-source runs.

A JobRun is the parent record the Jobs view rolls up: it groups the child
``ExtractionRun`` rows (one per assigned source) created when the job fired, and
its status reflects their aggregate outcome. Reconciled by the scheduler tick.
"""

import uuid
from datetime import datetime
from enum import Enum

from sqlalchemy import (
    DateTime, Enum as SAEnum, ForeignKey, Integer, String, func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class JobRunStatus(str, Enum):
    PENDING = "pending"      # children enqueued, none started yet
    RUNNING = "running"      # at least one child running, not all terminal
    COMPLETED = "completed"  # all children completed
    PARTIAL = "partial"      # some children completed, some failed/cancelled
    FAILED = "failed"        # all children failed
    CANCELLED = "cancelled"  # all children cancelled


class JobRun(Base):
    __tablename__ = "job_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[JobRunStatus] = mapped_column(
        SAEnum(JobRunStatus), default=JobRunStatus.PENDING, nullable=False
    )
    trigger: Mapped[str] = mapped_column(String(16), default="scheduled", nullable=False)

    sources_total: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    sources_done: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    sources_failed: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    job: Mapped["Job"] = relationship("Job", back_populates="runs")
    runs: Mapped[list["ExtractionRun"]] = relationship(
        "ExtractionRun", back_populates="job_run"
    )
