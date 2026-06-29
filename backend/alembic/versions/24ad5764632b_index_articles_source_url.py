"""index articles.source_url

The post-scrape TOC rebuild re-links every article by source_url, and
resume/reconcile look articles up by URL; without an index these are full-table
scans. Idempotent (CREATE INDEX IF NOT EXISTS / DROP INDEX IF EXISTS).

Revision ID: 24ad5764632b
Revises: 241b44ba5ea6
Create Date: 2026-06-29
"""
from alembic import op

revision = "24ad5764632b"
down_revision = "241b44ba5ea6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_articles_source_url "
        "ON articles (source_url)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_articles_source_url")
