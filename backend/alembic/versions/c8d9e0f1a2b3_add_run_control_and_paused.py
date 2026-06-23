"""add extraction_runs.control + RunStatus.PAUSED

Adds a cooperative control signal column ("cancel" | "pause") and a new PAUSED
value to the runstatus enum, for run cancel/pause/resume.

Revision ID: c8d9e0f1a2b3
Revises: b7c8d9e0f1a2
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c8d9e0f1a2b3"
down_revision: Union[str, Sequence[str], None] = "b7c8d9e0f1a2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "extraction_runs",
        sa.Column("control", sa.String(16), nullable=True),
    )
    # ADD VALUE must run outside the migration's transaction (Postgres won't let
    # a newly-added enum value be used in the same transaction). autocommit_block
    # runs it standalone. IF NOT EXISTS makes the migration idempotent.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE runstatus ADD VALUE IF NOT EXISTS 'PAUSED'")


def downgrade() -> None:
    op.drop_column("extraction_runs", "control")
    # Postgres has no DROP VALUE; the orphaned 'PAUSED' enum label is harmless
    # and left in place.
