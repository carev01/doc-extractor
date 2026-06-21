"""add toc_checkpoints

Revision ID: a2b3c4d5e6f8
Revises: a1b2c3d4e5f7
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "a2b3c4d5e6f8"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "toc_checkpoints",
        sa.Column(
            "source_id",
            UUID(as_uuid=True),
            sa.ForeignKey("documentation_sources.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("data", JSONB, nullable=False, server_default="{}"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("toc_checkpoints")
