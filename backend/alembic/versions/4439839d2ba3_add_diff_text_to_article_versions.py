"""add diff_text to article_versions

Revision ID: 4439839d2ba3
Revises: 6601fe70d971
Create Date: 2026-06-17 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4439839d2ba3'
down_revision: Union[str, Sequence[str], None] = '6601fe70d971'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('article_versions', sa.Column('diff_text', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('article_versions', 'diff_text')
