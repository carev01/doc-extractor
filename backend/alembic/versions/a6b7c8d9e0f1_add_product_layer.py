"""add product layer (Vendor -> Product -> Source)

Introduces a ``products`` table between vendors and documentation_sources.
Backfill is non-destructive: one product per existing source (named after the
source, carrying the source's vendor). After backfill, ``product_id`` becomes
NOT NULL and the now-redundant ``documentation_sources.vendor_id`` is dropped
(a source's vendor is reached via its product).

Revision ID: a6b7c8d9e0f1
Revises: a2b3c4d5e6f8
"""
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "a6b7c8d9e0f1"
down_revision: Union[str, Sequence[str], None] = "a2b3c4d5e6f8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. products table.
    op.create_table(
        "products",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "vendor_id",
            UUID(as_uuid=True),
            sa.ForeignKey("vendors.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(512), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_products_vendor_id", "products", ["vendor_id"])

    # 2. product_id on sources (nullable until backfilled).
    op.add_column(
        "documentation_sources",
        sa.Column(
            "product_id",
            UUID(as_uuid=True),
            sa.ForeignKey("products.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )

    # 3. Backfill: one product per source, carrying the source's vendor + name.
    #    UUIDs are passed as text with explicit ::uuid casts so this works the
    #    same regardless of the DBAPI's uuid adapter.
    conn = op.get_bind()
    sources = conn.execute(
        sa.text("SELECT id::text AS id, vendor_id::text AS vid, name FROM documentation_sources")
    ).fetchall()
    ins = sa.text(
        "INSERT INTO products (id, vendor_id, name, created_at, updated_at) "
        "VALUES (:id::uuid, :vid::uuid, :name, now(), now())"
    )
    upd = sa.text(
        "UPDATE documentation_sources SET product_id = :pid::uuid WHERE id = :sid::uuid"
    )
    for s in sources:
        pid = str(uuid.uuid4())
        conn.execute(ins, {"id": pid, "vid": s.vid, "name": s.name})
        conn.execute(upd, {"pid": pid, "sid": s.id})

    # 4. Now every source has a product.
    op.alter_column("documentation_sources", "product_id", nullable=False)
    op.create_index(
        "ix_documentation_sources_product_id", "documentation_sources", ["product_id"]
    )

    # 5. Drop the now-redundant vendor_id (PG drops its FK with the column).
    op.drop_column("documentation_sources", "vendor_id")


def downgrade() -> None:
    # Re-add vendor_id, backfill from the product's vendor, make NOT NULL.
    op.add_column(
        "documentation_sources",
        sa.Column(
            "vendor_id",
            UUID(as_uuid=True),
            sa.ForeignKey("vendors.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.execute(
        "UPDATE documentation_sources s "
        "SET vendor_id = p.vendor_id FROM products p WHERE s.product_id = p.id"
    )
    op.alter_column("documentation_sources", "vendor_id", nullable=False)

    op.drop_index(
        "ix_documentation_sources_product_id", table_name="documentation_sources"
    )
    op.drop_column("documentation_sources", "product_id")
    op.drop_index("ix_products_vendor_id", table_name="products")
    op.drop_table("products")
