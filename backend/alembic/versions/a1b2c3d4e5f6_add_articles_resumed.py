"""add extraction_runs.articles_resumed

Counts pages carried over from a prior interrupted attempt (resume checkpoint),
kept separate from the new/updated/unchanged breakdown.

Revision ID: a1b2c3d4e5f6
Revises: 9ad7b7dc0fc7
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "9ad7b7dc0fc7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "extraction_runs",
        sa.Column(
            "articles_resumed",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("extraction_runs", "articles_resumed")
