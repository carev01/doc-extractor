"""ExtractionRun model — tracks each extraction execution."""

import uuid
from datetime import datetime
from enum import Enum

from sqlalchemy import DateTime, Enum as SAEnum, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class RunStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ExtractionRun(Base):
    __tablename__ = "extraction_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documentation_sources.id", ondelete="CASCADE"),
        nullable=False,
    )

    status: Mapped[RunStatus] = mapped_column(
        SAEnum(RunStatus), default=RunStatus.RUNNING, nullable=False
    )
    articles_extracted: Mapped[int] = mapped_column(Integer, default=0)
    articles_total: Mapped[int] = mapped_column(Integer, default=0)
    # Incremental extraction counters.
    articles_unchanged: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    articles_updated: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    error_message: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    # Firecrawl async batch job tracking.
    firecrawl_job_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    current_phase: Mapped[str | None] = mapped_column(String(64), nullable=True)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    source: Mapped["DocumentationSource"] = relationship(
        "DocumentationSource", back_populates="extraction_runs"
    )
    articles: Mapped[list["Article"]] = relationship(
        "Article", back_populates="extraction_run"
    )
