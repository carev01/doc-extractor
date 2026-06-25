"""add versioned url handling

Revision ID: 9ad7b7dc0fc7
Revises: d9e0f1a2b3c4
Create Date: 2026-06-25 01:13:04.855285

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9ad7b7dc0fc7'
down_revision: Union[str, Sequence[str], None] = 'd9e0f1a2b3c4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("products", sa.Column("version", sa.String(64), nullable=True))
    op.add_column("products", sa.Column("previous_version", sa.String(64), nullable=True))
    op.add_column("documentation_sources", sa.Column("url_template", sa.String(2048), nullable=True))
    op.add_column("extraction_runs", sa.Column("version", sa.String(64), nullable=True))
    op.add_column("articles", sa.Column("topic_key", sa.String(2048), nullable=True))
    # Backfill existing rows so the matching key is unchanged for non-versioned sources.
    op.execute("UPDATE articles SET topic_key = source_url WHERE topic_key IS NULL")
    op.alter_column("articles", "topic_key", nullable=False)
    # After backfill topic_key == source_url, so this unique constraint assumes no
    # pre-existing duplicate (source_id, source_url) rows. There has never been a
    # DB-level constraint on that pair, but the article upsert dedupes by it, so
    # production has none (verified: 0 duplicate groups across ~51.8k rows). If a
    # future dataset has dups, this create_unique_constraint fails loudly — dedupe
    # before upgrading.
    op.create_unique_constraint(
        "uq_articles_source_topic", "articles", ["source_id", "topic_key"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_articles_source_topic", "articles", type_="unique")
    op.drop_column("articles", "topic_key")
    op.drop_column("extraction_runs", "version")
    op.drop_column("documentation_sources", "url_template")
    op.drop_column("products", "previous_version")
    op.drop_column("products", "version")
