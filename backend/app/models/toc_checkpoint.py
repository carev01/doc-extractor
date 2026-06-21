"""TOC build checkpoint.

A large lazy sidebar (e.g. Commvault's ~9,670-node tree) is expanded one
top-level section at a time. Each completed section is persisted here so that if
the worker/pod is interrupted mid-build, the requeued run resumes from the
sections already done instead of restarting the ~14-minute walk from zero.

One row per source (a source has at most one active run, see
``uq_active_run_per_source``). The row is deleted once the full TOC is assembled.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class TocCheckpoint(Base):
    __tablename__ = "toc_checkpoints"

    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documentation_sources.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # {"top_level": [<section node>, ...], "sections": {section_id: [<node>, ...]}}
    data: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
