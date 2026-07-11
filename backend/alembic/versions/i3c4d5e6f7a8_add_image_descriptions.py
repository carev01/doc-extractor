"""add image_descriptions cache + ArticleImage description columns

Revision ID: i3c4d5e6f7a8
Revises: h2b3c4d5e6f7
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "i3c4d5e6f7a8"
down_revision: Union[str, Sequence[str], None] = "h2b3c4d5e6f7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("article_images", sa.Column("is_meaningful", sa.Boolean(), nullable=True))
    op.add_column("article_images", sa.Column("description", sa.String(4096), nullable=True))
    op.add_column("article_images", sa.Column("kind", sa.String(16), nullable=True))
    op.add_column("article_images", sa.Column("width", sa.Integer(), nullable=True))
    op.add_column("article_images", sa.Column("height", sa.Integer(), nullable=True))
    op.add_column("article_images", sa.Column("bytes_sha256", sa.String(64), nullable=True))
    op.create_index("ix_article_images_bytes_sha256", "article_images", ["bytes_sha256"])

    op.create_table(
        "image_descriptions",
        sa.Column("bytes_sha256", sa.String(64), primary_key=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=True),
        sa.Column("model", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("image_descriptions")
    op.drop_index("ix_article_images_bytes_sha256", table_name="article_images")
    for col in ("bytes_sha256", "height", "width", "kind", "description", "is_meaningful"):
        op.drop_column("article_images", col)
