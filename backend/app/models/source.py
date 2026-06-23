"""DocumentationSource model — a specific doc site to extract."""

import uuid
from datetime import datetime
from enum import Enum

from sqlalchemy import DateTime, Enum as SAEnum, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class SourceStatus(str, Enum):
    PENDING = "pending"
    EXTRACTING = "extracting"
    COMPLETED = "completed"
    FAILED = "failed"


class DocumentationSource(Base):
    __tablename__ = "documentation_sources"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    base_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    status: Mapped[SourceStatus] = mapped_column(
        SAEnum(SourceStatus), default=SourceStatus.PENDING, nullable=False
    )
    last_extracted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error_message: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    # Extraction platform profile (e.g. "lazy_tree", "docusaurus", "intercom").
    # NULL = not yet detected; "generic" = sitemap fallback. Set by detection or UI override.
    platform: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Optional per-source overrides / LLM-derived selectors for the profile.
    profile_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    product: Mapped["Product"] = relationship("Product", back_populates="sources")
    extraction_runs: Mapped[list["ExtractionRun"]] = relationship(
        "ExtractionRun", back_populates="source", cascade="all, delete-orphan"
    )
    articles: Mapped[list["Article"]] = relationship(
        "Article", back_populates="source", cascade="all, delete-orphan"
    )
    toc_entries: Mapped[list["TOCEntry"]] = relationship(
        "TOCEntry", back_populates="source", cascade="all, delete-orphan"
    )
