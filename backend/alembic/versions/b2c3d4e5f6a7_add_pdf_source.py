"""add documentation_sources.source_type + extraction_runs.pdf_hash

Revision ID: b2c3d4e5f6a7
Revises: f1e2d3c4b5a6
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, Sequence[str], None] = "f1e2d3c4b5a6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "documentation_sources",
        sa.Column("source_type", sa.String(16), nullable=False, server_default="web"),
    )
    op.add_column(
        "extraction_runs",
        sa.Column("pdf_hash", sa.String(64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("extraction_runs", "pdf_hash")
    op.drop_column("documentation_sources", "source_type")
