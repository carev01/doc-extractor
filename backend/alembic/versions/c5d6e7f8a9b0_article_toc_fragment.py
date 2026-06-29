"""add articles.toc_fragment

Revision ID: c5d6e7f8a9b0
Revises: 17c13db3546c
Create Date: 2026-06-29

"""
from alembic import op
import sqlalchemy as sa

revision = "c5d6e7f8a9b0"
down_revision = "17c13db3546c"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.add_column("articles", sa.Column("toc_fragment", sa.Text(), nullable=True))

def downgrade() -> None:
    op.drop_column("articles", "toc_fragment")
