"""Article model — a single page/article extracted from a doc source."""

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class Article(Base):
    __tablename__ = "articles"

    __table_args__ = (
        UniqueConstraint("source_id", "topic_key", name="uq_articles_source_topic"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documentation_sources.id", ondelete="CASCADE"),
        nullable=False,
    )
    extraction_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("extraction_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    # The run that first created this article — distinguishes the baseline
    # (first) extraction from later incremental additions in the changelog.
    created_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("extraction_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    toc_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("toc_entries.id", ondelete="SET NULL"),
        nullable=True,
        # Indexed: deleting toc_entries (every TOC rebuild / re-run) fires the
        # ON DELETE SET NULL back-reference; without this index that is a full
        # articles scan per deleted row (O(n*m)).
        index=True,
    )

    # Removal tracking — stamped when the page first drops out of the rebuilt
    # TOC, cleared if it returns. Drives the changelog "removed" events.
    removed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    removal_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("extraction_runs.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Content
    title: Mapped[str] = mapped_column(String(1024), nullable=False)
    # Indexed: the post-scrape TOC rebuild re-links every article by source_url,
    # and resume/reconcile look articles up by URL.
    source_url: Mapped[str] = mapped_column(String(2048), nullable=False, index=True)
    # Version-independent topic identity; unique per source. Equals source_url
    # for non-versioned sources.
    topic_key: Mapped[str] = mapped_column(String(2048), nullable=False)
    content_markdown: Mapped[str] = mapped_column(Text, nullable=False)
    content_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Captured TOC-tree fragment (e.g. Oxygen WebHelp #wh_publication_toc outerHTML)
    # used to post-process the authored hierarchy without re-fetching. Nullable;
    # populated only by profiles that set ``toc_fragment_selector``.
    toc_fragment: Mapped[str | None] = mapped_column(Text, nullable=True)
    # SHA-256 hex digest of content_markdown — used to detect changes
    # between extraction runs for incremental extraction.
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # NOTE: ``search_vector`` is a PostgreSQL GENERATED ALWAYS tsvector column
    # created by the Alembic migration (fts5_search_vector).  It is NOT declared
    # as a SQLAlchemy mapped column because the DB owns it entirely — SQLAlchemy
    # must never try to INSERT or UPDATE it.  Queries reference it via
    # ``Article.search_vector`` using the ``text()`` construct or via the
    # ``func.plainto_tsquery`` / ``@@`` operators in the route layer.

    # Metadata
    last_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    estimated_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    content_size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Timestamps
    extracted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    source: Mapped["DocumentationSource"] = relationship(
        "DocumentationSource", back_populates="articles"
    )
    extraction_run: Mapped["ExtractionRun | None"] = relationship(
        "ExtractionRun",
        back_populates="articles",
        foreign_keys="[Article.extraction_run_id]",
    )
    toc_entry: Mapped["TOCEntry | None"] = relationship(
        "TOCEntry", back_populates="articles"
    )
    images: Mapped[list["ArticleImage"]] = relationship(
        "ArticleImage", back_populates="article", cascade="all, delete-orphan"
    )
    versions: Mapped[list["ArticleVersion"]] = relationship(
        "ArticleVersion", back_populates="article", cascade="all, delete-orphan"
    )