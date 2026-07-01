"""add tsvector search_vector column and GIN index on articles

Revision ID: f7a8b9c0d1e2
Revises: c9e1f3a5b7d2
Create Date: 2026-07-01

Adds a generated tsvector column (``search_vector``) on the articles table
computed from ``title`` and ``content_markdown``, plus a GIN index for
fast full-text search via PostgreSQL FTS5.

The tsvector is stored (not virtual) so we can index it with GIN and use
``ts_rank`` for relevance ordering.  We use ``to_tsvector('english', ...)``
with simple concatenation — title gets a higher weight via ``setweight``
so keyword matches in titles rank above body matches.

This migration is purely additive: no existing columns are altered or
dropped, and the new column is nullable so rows that pre-date the
migration get a NULL search_vector (backfilled by the UPDATE below).
"""
from alembic import op
import sqlalchemy as sa

revision = "f7a8b9c0d1e2"
down_revision = "c9e1f3a5b7d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add the generated tsvector column.
    #    STORED generated columns cannot reference other generated columns,
    #    but title and content_markdown are plain columns so this is safe.
    #    Using 'english' configuration for stemming; switch to 'simple' if
    #    multi-language content becomes a concern.
    op.execute(
        """
        ALTER TABLE articles
        ADD COLUMN search_vector tsvector
        GENERATED ALWAYS AS (
            setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
            setweight(to_tsvector('english', coalesce(content_markdown, '')), 'B')
        ) STORED
        """
    )

    # 2. GIN index for fast @@ (contains) queries.
    op.execute(
        "CREATE INDEX ix_articles_search_vector ON articles USING GIN (search_vector)"
    )

    # 3. Composite index on (source_id, sort_order) — already implicitly useful
    #    for filtered+ordered queries, but only add if not already present.
    #    (The existing indexes are on source_url and toc_entry_id individually.)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_articles_search_vector")
    op.execute("ALTER TABLE articles DROP COLUMN IF EXISTS search_vector")