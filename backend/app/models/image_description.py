"""ImageDescription — content-hash-keyed cache of VLM image descriptions.

Shared across all articles and sources: a given image's bytes are described once,
ever. Keyed by the SHA-256 of the image bytes so an identical image reused on many
pages (or re-downloaded on a later run) reuses the cached description.
"""

from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ImageDescription(Base):
    __tablename__ = "image_descriptions"

    bytes_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Which model produced it — lets a future backfill re-describe stale-model rows.
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
