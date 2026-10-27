"""Index article_versions.article_id and article_images.article_id.

Both are ON DELETE CASCADE FKs to articles, so every deleted article makes
Postgres look up its versions and images — without an index, a sequential scan of
each whole table (276 MB of versions in production) once per deleted row. On a
production copy the NetBackup split's bulk delete of ~14.5k articles ran past ten
minutes before it was stopped; with these indexes the whole split took 22 seconds.
The same lookups serve every per-article image and version read.

Built CONCURRENTLY (outside a transaction) so a deploy doesn't block an
extraction writing these tables, like fa1b2c3d4e5f; an interrupted concurrent
build leaves an INVALID index, which is dropped and rebuilt rather than kept.

Revision ID: a7b8c9d0e1f2
Revises: fa1b2c3d4e5f
"""

from alembic import op

revision = "a7b8c9d0e1f2"
down_revision = "fa1b2c3d4e5f"
branch_labels = None
depends_on = None

_INDEXES = {
    "ix_article_versions_article_id": "article_versions",
    "ix_article_images_article_id": "article_images",
}


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for name, table in _INDEXES.items():
            op.execute(f"""
                DO $$
                BEGIN
                    IF EXISTS (
                        SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
                         WHERE c.relname = '{name}' AND NOT i.indisvalid
                    ) THEN
                        EXECUTE 'DROP INDEX {name}';
                    END IF;
                END $$;
            """)
            op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {table} (article_id)")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name in _INDEXES:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
