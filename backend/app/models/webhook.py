"""WebhookConfig model — user-configured outbound webhook endpoints.

Each row is a destination URL that receives a POST with a JSON payload when
content-change events fire (new page, updated page, extraction complete, page
removed). Payloads are signed with an HMAC-SHA256 signature so receivers can
verify authenticity.
"""

import uuid
from datetime import datetime
from enum import Enum

from sqlalchemy import (
    Boolean, DateTime, Enum as SAEnum, ForeignKey, Integer, String, Text, func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class WebhookEventType(str, Enum):
    NEW_PAGE = "new_page"
    UPDATED_PAGE = "updated_page"
    REMOVED_PAGE = "removed_page"
    EXTRACTION_COMPLETE = "extraction_complete"


class WebhookConfig(Base):
    __tablename__ = "webhooks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Optional scope: NULL = global (all sources); a source_id = scoped to that
    # source only. FK SET NULL so deleting a source doesn't delete its webhooks.
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documentation_sources.id", ondelete="SET NULL"),
        nullable=True,
    )
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    # Human-readable label for the UI.
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Comma-separated list of event types this webhook subscribes to.
    # Stored as a plain string for simplicity (queried as substring match).
    events: Mapped[str] = mapped_column(
        String(255), default="extraction_complete", server_default="extraction_complete",
        nullable=False,
    )
    # HMAC secret used to sign payloads. NULL = no signing (insecure, but
    # allowed for local dev). Receivers verify via X-DocExtractor-Signature.
    secret: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true", nullable=False
    )
    # Delivery stats (best-effort, updated by the dispatcher).
    last_status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    total_deliveries: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    total_failures: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    source: Mapped["DocumentationSource | None"] = relationship(
        "DocumentationSource", backref="webhooks"
    )
    deliveries: Mapped[list["WebhookDelivery"]] = relationship(
        "WebhookDelivery", back_populates="webhook", cascade="all, delete-orphan"
    )


class WebhookDelivery(Base):
    """Delivery attempt log for auditing and retry inspection."""

    __tablename__ = "webhook_deliveries"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    webhook_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("webhooks.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # The extraction run that triggered this delivery (NULL for test pings).
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("extraction_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documentation_sources.id", ondelete="SET NULL"),
        nullable=True,
    )
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    # success = True when the receiver returned 2xx.
    success: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    webhook: Mapped["WebhookConfig"] = relationship(
        "WebhookConfig", back_populates="deliveries"
    )