"""add extraction_runs.allow_toc_collapse

Per-run override of the TOC-collapse data-loss guard, so an operator can extract a
doc set that genuinely shrank (or whose baseline is stale) without disabling the
guard for every source.

Revision ID: c4d5e6f7a8b9
Revises: f8a9b0c1d2e3
Create Date: 2026-08-18 00:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4d5e6f7a8b9'
down_revision: Union[str, Sequence[str], None] = 'f8a9b0c1d2e3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "extraction_runs",
        sa.Column(
            "allow_toc_collapse",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("extraction_runs", "allow_toc_collapse")
