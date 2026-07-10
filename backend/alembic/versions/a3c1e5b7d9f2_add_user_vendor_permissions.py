"""Add user_vendor_permissions table (per-vendor row-level authorization).

Revision ID: a3c1e5b7d9f2
Revises: 98eff2457242
Create Date: 2026-07-10

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "a3c1e5b7d9f2"
down_revision: Union[str, None] = "98eff2457242"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create the enum once; the column uses create_type=False so create_table
    # doesn't try to emit CREATE TYPE a second time.
    sa.Enum("read_only", "read_write", name="vendoraccesslevel").create(
        op.get_bind(), checkfirst=True
    )
    level = postgresql.ENUM(
        "read_only", "read_write", name="vendoraccesslevel", create_type=False
    )

    op.create_table(
        "user_vendor_permissions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "vendor_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("vendors.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("level", level, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.UniqueConstraint("user_id", "vendor_id", name="uq_user_vendor"),
    )
    op.create_index("ix_user_vendor_permissions_user_id", "user_vendor_permissions", ["user_id"])
    op.create_index("ix_user_vendor_permissions_vendor_id", "user_vendor_permissions", ["vendor_id"])


def downgrade() -> None:
    op.drop_index("ix_user_vendor_permissions_vendor_id", table_name="user_vendor_permissions")
    op.drop_index("ix_user_vendor_permissions_user_id", table_name="user_vendor_permissions")
    op.drop_table("user_vendor_permissions")
    sa.Enum(name="vendoraccesslevel").drop(op.get_bind(), checkfirst=True)
