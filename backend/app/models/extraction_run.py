"""ExtractionRun model — tracks each extraction execution."""

import uuid
from datetime import datetime
from enum import Enum

from sqlalchemy import (
    Boolean, DateTime, Enum as SAEnum, ForeignKey, Index, Integer, String, Text,
    func, text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PAUSED = "paused"


class ExtractionRun(Base):
    __tablename__ = "extraction_runs"

    __table_args__ = (
        Index(
            "ix_runs_pending", "created_at",
            postgresql_where=text("status = 'PENDING'"),
        ),
        Index(
            "uq_active_run_per_source", "source_id", unique=True,
            postgresql_where=text("status IN ('PENDING', 'RUNNING')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documentation_sources.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Parent JobRun when this run was created by a job fan-out (NULL for ad-hoc
    # manual runs). SET NULL so deleting job history never deletes the run.
    job_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("job_runs.id", ondelete="SET NULL"),
        nullable=True,
    )

    status: Mapped[RunStatus] = mapped_column(
        SAEnum(RunStatus), default=RunStatus.RUNNING, nullable=False
    )
    # Queue / worker-coordination columns.
    trigger: Mapped[str] = mapped_column(String(16), default="manual", server_default="manual", nullable=False)
    # Run kind: "extract" (full pipeline) | "escalate" (PDF VLM-escalation-only
    # retry — re-converts just the page ranges that failed escalation, skipping
    # the expensive Layer-A conversion) | "enrich" (image-description-only run) |
    # "retry_blocked" (re-scrape only the pages a prior run recorded as bot-blocked,
    # no TOC re-discovery). The worker dispatches on this.
    kind: Mapped[str] = mapped_column(String(16), default="extract", server_default="extract", nullable=False)
    claimed_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    # Earliest time this PENDING run may be claimed. Set when a run is requeued
    # with backoff after a retryable failure (e.g. an intermittent PDF download);
    # claim_next_run skips runs whose next_attempt_at is still in the future, so a
    # transiently-failing source is retried later without blocking the queue.
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Product version captured at run time (NULL for non-versioned products).
    version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Per-run override of the TOC-collapse data-loss guard. Set only by an explicit
    # "Extract anyway" trigger, so a genuinely-shrunken doc set (or a stale
    # baseline) can be extracted without disabling the guard globally. One-shot: it
    # lives on this run, so the next ordinary/scheduled run is protected again.
    allow_toc_collapse: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )

    articles_extracted: Mapped[int] = mapped_column(Integer, default=0)
    articles_total: Mapped[int] = mapped_column(Integer, default=0)
    articles_unchanged: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    articles_updated: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    articles_resumed: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    # SHA-256 of the PDF bytes for a pdf source (NULL for web runs); lets a
    # re-run fast-path to "all unchanged" when the PDF is byte-identical.
    pdf_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    firecrawl_job_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    current_phase: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # PDF pages whose VLM escalation still needs to run (a service outage, or over
    # the per-run page budget), retryable without redoing the conversion: a list of
    # {page_start, page_end} 0-based inclusive page ranges. Non-empty ⇒ the run
    # completed with an escalation *warning* (not a clean green) and is eligible for
    # a kind="escalate" retry (which reuses the cached converted doc). NULL/empty ⇒
    # no warning.
    escalation_pending: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    # Pages that tripped bot protection (Akamai/Cloudflare) and were not stored
    # this run: a list of {url, title, toc_entry_id, sort_order, topic_key}.
    # Accumulated during the content phase; after the optional auto-retry pass,
    # a non-empty list ⇒ the run completed with a bot-protection *warning* (not a
    # clean green) and is eligible for a kind="retry_blocked" retry. NULL/empty ⇒
    # no warning. Capped to avoid an unbounded list on a fully-blocked run.
    blocked_pending: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    # Cooperative control signal set by the API ("cancel" | "pause"); the worker
    # observes it at batch boundaries and transitions the run accordingly, then
    # clears it. NULL = no pending control request.
    control: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Captured worker logs for this run (tail-capped). Populated by the worker's
    # per-run log handler so the UI can show raw logs without kubectl.
    log_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    source: Mapped["DocumentationSource"] = relationship(
        "DocumentationSource", back_populates="extraction_runs"
    )
    job_run: Mapped["JobRun | None"] = relationship(
        "JobRun", back_populates="runs"
    )
    articles: Mapped[list["Article"]] = relationship(
        "Article",
        back_populates="extraction_run",
        foreign_keys="[Article.extraction_run_id]",
    )
