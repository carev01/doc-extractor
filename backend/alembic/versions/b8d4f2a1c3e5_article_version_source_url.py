"""add article_versions.source_url

Revision ID: b8d4f2a1c3e5
Revises: 24ad5764632b
Create Date: 2026-06-30

"""
from alembic import op
import sqlalchemy as sa

revision = "b8d4f2a1c3e5"
down_revision = "24ad5764632b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "article_versions",
        sa.Column("source_url", sa.String(length=2048), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("article_versions", "source_url")
