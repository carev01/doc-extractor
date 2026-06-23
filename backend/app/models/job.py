"""Job model — a named group of sources with a shared (optional) schedule.

Modelled on backup jobs (e.g. Veeam): a job owns a set of documentation
sources and one schedule. When the job fires it fans out into one extraction
run per assigned source, grouped under a ``JobRun`` for monitoring. Scheduling
lives here, not on individual sources (a source is assigned to at most one job).
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, func
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
    frequency: Mapped[str | None] = mapped_column(String(16), nullable=True)  # hourly|daily|weekly|monthly
    time_of_day: Mapped[str | None] = mapped_column(String(5), nullable=True)  # HH:MM
    day_of_week: Mapped[int | None] = mapped_column(Integer, nullable=True)    # 0-6, 0=Sun (weekly)
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

    # Assigned sources (one job per source; FK on the source side, SET NULL on
    # job delete so deleting a job just un-assigns its sources).
    sources: Mapped[list["DocumentationSource"]] = relationship(
        "DocumentationSource", back_populates="job"
    )
    runs: Mapped[list["JobRun"]] = relationship(
        "JobRun", back_populates="job", cascade="all, delete-orphan"
    )
