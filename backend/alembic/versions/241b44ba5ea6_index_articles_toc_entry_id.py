"""index articles.toc_entry_id

Deleting toc_entries (every TOC rebuild / re-run) fires the ON DELETE SET NULL
back-reference on articles.toc_entry_id; without an index that is a full articles
scan per deleted row (O(n*m)), which made TOC rebuilds hang on large corpora.

Idempotent: the index may already exist (created out-of-band on a live DB to
unblock a run), so use IF NOT EXISTS / IF EXISTS.

Revision ID: 241b44ba5ea6
Revises: c5d6e7f8a9b0
Create Date: 2026-06-29
"""
from alembic import op

revision = "241b44ba5ea6"
down_revision = "c5d6e7f8a9b0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_articles_toc_entry_id "
        "ON articles (toc_entry_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_articles_toc_entry_id")
