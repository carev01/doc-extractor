"""add content_changes outbox

Revision ID: h2b3c4d5e6f7
Revises: a3c1e5b7d9f2
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "h2b3c4d5e6f7"
down_revision: Union[str, Sequence[str], None] = "a3c1e5b7d9f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "content_changes",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "article_id",
            UUID(as_uuid=True),
            sa.ForeignKey("articles.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "source_id",
            UUID(as_uuid=True),
            sa.ForeignKey("documentation_sources.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("extraction_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("change_type", sa.String(16), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=True),
        sa.Column("topic_key", sa.String(2048), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_content_changes_run_id", "content_changes", ["run_id"])
    op.create_index("ix_content_changes_source_id_id", "content_changes", ["source_id", "id"])


def downgrade() -> None:
    op.drop_index("ix_content_changes_source_id_id", table_name="content_changes")
    op.drop_index("ix_content_changes_run_id", table_name="content_changes")
    op.drop_table("content_changes")
