"""add extraction_runs.log_text for per-run captured logs

Revision ID: b7c8d9e0f1a2
Revises: a6b7c8d9e0f1
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b7c8d9e0f1a2"
down_revision: Union[str, Sequence[str], None] = "a6b7c8d9e0f1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "extraction_runs",
        sa.Column("log_text", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("extraction_runs", "log_text")
