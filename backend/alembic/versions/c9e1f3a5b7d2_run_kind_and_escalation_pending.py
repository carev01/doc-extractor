"""add extraction_runs.kind and escalation_pending

Revision ID: c9e1f3a5b7d2
Revises: b8d4f2a1c3e5
Create Date: 2026-06-30

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "c9e1f3a5b7d2"
down_revision = "b8d4f2a1c3e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "extraction_runs",
        sa.Column("kind", sa.String(length=16), server_default="extract", nullable=False),
    )
    op.add_column(
        "extraction_runs",
        sa.Column("escalation_pending", JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("extraction_runs", "escalation_pending")
    op.drop_column("extraction_runs", "kind")
