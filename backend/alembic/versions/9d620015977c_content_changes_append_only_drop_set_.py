"""content_changes append-only (drop SET NULL FKs)

Revision ID: 9d620015977c
Revises: i3c4d5e6f7a8
Create Date: 2026-07-11 23:21:22.833926

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9d620015977c'
down_revision: Union[str, Sequence[str], None] = 'i3c4d5e6f7a8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_constraint("content_changes_article_id_fkey", "content_changes", type_="foreignkey")
    op.drop_constraint("content_changes_source_id_fkey", "content_changes", type_="foreignkey")
    op.drop_constraint("content_changes_run_id_fkey", "content_changes", type_="foreignkey")


def downgrade() -> None:
    """Downgrade schema."""
    op.create_foreign_key("content_changes_run_id_fkey", "content_changes",
                          "extraction_runs", ["run_id"], ["id"], ondelete="SET NULL")
    op.create_foreign_key("content_changes_source_id_fkey", "content_changes",
                          "documentation_sources", ["source_id"], ["id"], ondelete="SET NULL")
    op.create_foreign_key("content_changes_article_id_fkey", "content_changes",
                          "articles", ["article_id"], ["id"], ondelete="SET NULL")
