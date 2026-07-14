"""add extraction_runs.next_attempt_at

Backoff-and-retry for transient failures (notably intermittent PDF downloads,
e.g. Dell). When a run fails with a retryable error, the worker puts it back on
the queue as PENDING with next_attempt_at set to a future time; claim_next_run
skips runs whose next_attempt_at hasn't arrived, so the failing source is retried
later without blocking the other sources.

Revision ID: e7f8a9b0c1d2
Revises: d5f7a9c1b3e6
Create Date: 2026-07-14

"""
from alembic import op
import sqlalchemy as sa

revision = "e7f8a9b0c1d2"
down_revision = "d5f7a9c1b3e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "extraction_runs",
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("extraction_runs", "next_attempt_at")
