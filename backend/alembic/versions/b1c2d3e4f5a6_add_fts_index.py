"""add full-text search GIN index on articles

Revision ID: b1c2d3e4f5a6
Revises: 4439839d2ba3
Create Date: 2026-06-17 16:30:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'b1c2d3e4f5a6'
down_revision: Union[str, Sequence[str], None] = '4439839d2ba3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Must match exporter._TSV exactly so the planner uses this index for topic search.
_TSV = (
    "to_tsvector('english', coalesce(title,'') || ' ' || coalesce(content_markdown,''))"
)


def upgrade() -> None:
    op.execute(
        f"CREATE INDEX IF NOT EXISTS ix_articles_fts ON articles USING GIN ({_TSV})"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_articles_fts")
