"""add article created_run_id

Revision ID: e4f5a6b7c8d9
Revises: d3e4f5a6b7c8
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "e4f5a6b7c8d9"
down_revision: Union[str, Sequence[str], None] = "d3e4f5a6b7c8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "articles",
        sa.Column(
            "created_run_id", UUID(as_uuid=True),
            sa.ForeignKey("extraction_runs.id", ondelete="SET NULL"), nullable=True,
        ),
    )
    # Backfill: attribute each existing article to the run that was active when
    # it was created (the most recent run started at/before created_at).
    op.execute(
        "UPDATE articles a SET created_run_id = ("
        "  SELECT r.id FROM extraction_runs r"
        "  WHERE r.source_id = a.source_id AND r.started_at <= a.created_at"
        "  ORDER BY r.started_at DESC LIMIT 1"
        ") WHERE created_run_id IS NULL"
    )
    # Fallback for any straggler (created_at predates every run): attribute it to
    # the earliest run that already created articles for the source — i.e. the
    # baseline run — so stragglers don't masquerade as later additions.
    op.execute(
        "UPDATE articles a SET created_run_id = ("
        "  SELECT a2.created_run_id FROM articles a2"
        "  JOIN extraction_runs r ON r.id = a2.created_run_id"
        "  WHERE a2.source_id = a.source_id AND a2.created_run_id IS NOT NULL"
        "  ORDER BY r.started_at ASC LIMIT 1"
        ") WHERE created_run_id IS NULL"
    )


def downgrade() -> None:
    op.drop_column("articles", "created_run_id")
