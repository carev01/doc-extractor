"""add article_versions.source_url

Revision ID: a1b2c3d4e5f6
Revises: 24ad5764632b
Create Date: 2026-06-30

"""
from alembic import op
import sqlalchemy as sa

revision = "a1b2c3d4e5f6"
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
