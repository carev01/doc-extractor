"""Index toc_entries.parent_id.

toc_entries.parent_id is a self-referencing FK with ON DELETE SET NULL, so every
deleted TOC entry makes Postgres look up the entries that point at it. Without
an index that lookup is a sequential scan of the whole table (120k rows across
all sources) — once per deleted row. It fires on every source delete and on
every run's TOC rebuild, which replaces the source's whole TOC with a bulk
delete: deleting one source with 9,592 TOC entries took ~7 minutes.

Built CONCURRENTLY (outside a transaction) so a deploy doesn't block an
extraction that is writing toc_entries at the time. IF NOT EXISTS keeps it
re-runnable; a concurrent build that is interrupted leaves an INVALID index,
which is dropped and rebuilt here rather than silently kept.

Revision ID: fa1b2c3d4e5f
Revises: f9a0b1c2d3e4
"""

from alembic import op

revision = "fa1b2c3d4e5f"
down_revision = "f9a0b1c2d3e4"
branch_labels = None
depends_on = None

_INDEX = "ix_toc_entries_parent_id"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        # An interrupted CONCURRENTLY build leaves the index in place but INVALID;
        # IF NOT EXISTS would then keep the broken one, so clear it first.
        op.execute(f"""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
                     WHERE c.relname = '{_INDEX}' AND NOT i.indisvalid
                ) THEN
                    EXECUTE 'DROP INDEX {_INDEX}';
                END IF;
            END $$;
        """)
        op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX} ON toc_entries (parent_id)")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}")
