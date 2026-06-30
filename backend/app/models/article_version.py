"""ArticleVersion model — historical snapshots of article content.

Each time an article's content changes between extraction runs, the
previous content is preserved here before being overwritten, enabling
side-by-side comparison and changelogs later.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class ArticleVersion(Base):
    __tablename__ = "article_versions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    article_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("articles.id", ondelete="CASCADE"),
        nullable=False,
    )
    extraction_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("extraction_runs.id", ondelete="SET NULL"),
        nullable=True,
    )

    content_markdown: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    diff_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The article's source_url at the moment this snapshot was superseded. Lets a
    # previous version keep a working link to where its content lived even after
    # the live article's URL moves on — e.g. a from-URL PDF relocated to a new
    # host, or a version bump that re-pages every section. NULL for snapshots
    # captured before this column existed.
    source_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)

    extracted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relationships
    article: Mapped["Article"] = relationship("Article", back_populates="versions")
