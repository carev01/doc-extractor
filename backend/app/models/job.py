"""Job model — a named coverage (explicit targets and/or a vendor tier) with an
optional schedule.

When the job fires it fans out into one extraction run per covered source,
grouped under a ``JobRun`` for monitoring. Scheduling lives here, not on
individual sources.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(512), nullable=False)

    # Schedule. All schedule fields are nullable so a job can exist purely as a
    # manual group (enabled=False, no cron). When enabled, cron must be set —
    # enforced by the route, not the column.
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    schedule_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)  # daily|weekly|monthly_day|monthly_nth
    interval_months: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 1,2,3,4,6,12
    start_month: Mapped[int | None] = mapped_column(Integer, nullable=True)      # 1-12
    nth: Mapped[str | None] = mapped_column(String(8), nullable=True)            # first|last
    weekday: Mapped[int | None] = mapped_column(Integer, nullable=True)          # 0-6, 0=Sun
    tier_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tiers.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    time_of_day: Mapped[str | None] = mapped_column(String(5), nullable=True)  # HH:MM
    day_of_month: Mapped[int | None] = mapped_column(Integer, nullable=True)   # 1-28 (monthly)
    cron: Mapped[str | None] = mapped_column(String(128), nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", nullable=False)

    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    targets: Mapped[list["JobTarget"]] = relationship(
        "JobTarget", back_populates="job", cascade="all, delete-orphan", passive_deletes=True
    )
    exclusions: Mapped[list["JobExclusion"]] = relationship(
        "JobExclusion", back_populates="job", cascade="all, delete-orphan", passive_deletes=True
    )
    runs: Mapped[list["JobRun"]] = relationship(
        "JobRun", back_populates="job", cascade="all, delete-orphan"
    )
