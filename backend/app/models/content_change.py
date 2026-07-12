"""ContentChange model — append-only outbox of article change events.

One row is written, in the same transaction as the mutation, whenever an
article is added, updated, or removed during extraction. The BIGSERIAL ``id``
is the monotonic watermark the delta feed pages by; no timestamp drives
ordering. ``topic_key`` is copied onto the row so a removal tombstone stays
resolvable even after the article row is later hard-deleted. ``article_id``
is an unconstrained historical reference — it is a plain UUID column with no
foreign key, so a later hard delete of the article never nulls or cascades
this row.
"""

import uuid
from datetime import datetime
from enum import Enum

from sqlalchemy import BigInteger, DateTime, Index, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ChangeType(str, Enum):
    ADDED = "added"
    UPDATED = "updated"
    REMOVED = "removed"
    RUN_START = "run_start"


class ContentChange(Base):
    __tablename__ = "content_changes"

    __table_args__ = (
        # Safe-ceiling and run-summary lookups scan by run_id.
        Index("ix_content_changes_run_id", "run_id"),
        # Source-scoped feed scans page by (source_id, id).
        Index("ix_content_changes_source_id_id", "source_id", "id"),
    )

    # BIGSERIAL on Postgres — the monotonic watermark.
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    article_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    change_type: Mapped[str] = mapped_column(String(16), nullable=False)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    topic_key: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
