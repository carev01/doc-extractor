"""add extraction_runs.blocked_pending

Records the pages that tripped bot protection (Akamai/Cloudflare) and were not
stored during a run, so they can be auto-retried in a second pass or retried
manually later (kind="retry_blocked"). Mirrors escalation_pending.

Revision ID: d5f7a9c1b3e6
Revises: 9d620015977c
Create Date: 2026-07-12

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "d5f7a9c1b3e6"
down_revision = "9d620015977c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "extraction_runs",
        sa.Column("blocked_pending", JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("extraction_runs", "blocked_pending")
