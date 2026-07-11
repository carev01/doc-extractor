"""ArticleImage model — downloaded images referenced in articles."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class ArticleImage(Base):
    __tablename__ = "article_images"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    article_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("articles.id", ondelete="CASCADE"),
        nullable=False,
    )

    original_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    local_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    local_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    alt_text: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    file_size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    # Populated by the VLM image-description enrichment phase (opt-in).
    # is_meaningful: NULL = not yet evaluated; True/False = evaluated. description
    # is set only for meaningful images that have been described.
    is_meaningful: Mapped[bool | None] = mapped_column(nullable=True)
    description: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # SHA-256 of the image bytes — the cache key into image_descriptions and the
    # cross-article/source dedup key.
    bytes_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    article: Mapped["Article"] = relationship("Article", back_populates="images")
