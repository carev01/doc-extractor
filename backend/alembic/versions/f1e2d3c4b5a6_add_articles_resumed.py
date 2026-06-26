"""add extraction_runs.articles_resumed

Counts pages carried over from a prior interrupted attempt (resume checkpoint),
kept separate from the new/updated/unchanged breakdown.

Revision ID: f1e2d3c4b5a6
Revises: 9ad7b7dc0fc7
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f1e2d3c4b5a6"
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
