"""add article removal tracking

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "d3e4f5a6b7c8"
down_revision: Union[str, Sequence[str], None] = "c2d3e4f5a6b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("articles", sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "articles",
        sa.Column(
            "removal_run_id", UUID(as_uuid=True),
            sa.ForeignKey("extraction_runs.id", ondelete="SET NULL"), nullable=True,
        ),
    )
    # Backfill: any article already orphaned (dropped from the TOC) is a removal
    # we never recorded — stamp it at its last-seen time so it shows up.
    op.execute(
        "UPDATE articles SET removed_at = extracted_at "
        "WHERE toc_entry_id IS NULL AND removed_at IS NULL"
    )


def downgrade() -> None:
    op.drop_column("articles", "removal_run_id")
    op.drop_column("articles", "removed_at")
