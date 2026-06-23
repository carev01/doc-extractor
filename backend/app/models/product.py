"""Product model — a product whose documentation may span multiple sources.

Sits between Vendor and DocumentationSource: a vendor has many products, and a
product groups one or more documentation sources (each its own base_url, TOC,
runs, and versions). A source's vendor is reached through its product.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class Product(Base):
    __tablename__ = "products"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    vendor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vendors.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    vendor: Mapped["Vendor"] = relationship("Vendor", back_populates="products")
    sources: Mapped[list["DocumentationSource"]] = relationship(
        "DocumentationSource", back_populates="product", cascade="all, delete-orphan"
    )
