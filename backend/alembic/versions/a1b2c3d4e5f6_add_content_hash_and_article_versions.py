"""add content_hash and article versions

Revision ID: a1b2c3d4e5f6
Revises: 55103a48a0e8
Create Date: 2026-06-16 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = '55103a48a0e8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Article: content hash for incremental change detection
    op.add_column(
        'articles',
        sa.Column('content_hash', sa.String(length=64), nullable=True),
    )

    # ExtractionRun: incremental counters
    op.add_column(
        'extraction_runs',
        sa.Column(
            'articles_unchanged', sa.Integer(),
            nullable=False, server_default='0',
        ),
    )
    op.add_column(
        'extraction_runs',
        sa.Column(
            'articles_updated', sa.Integer(),
            nullable=False, server_default='0',
        ),
    )

    # Historical snapshots of article content
    op.create_table(
        'article_versions',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('article_id', sa.UUID(), nullable=False),
        sa.Column('extraction_run_id', sa.UUID(), nullable=True),
        sa.Column('content_markdown', sa.Text(), nullable=False),
        sa.Column('content_hash', sa.String(length=64), nullable=True),
        sa.Column(
            'extracted_at', sa.DateTime(timezone=True),
            server_default=sa.text('now()'), nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ['article_id'], ['articles.id'], ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['extraction_run_id'], ['extraction_runs.id'], ondelete='SET NULL',
        ),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('article_versions')
    op.drop_column('extraction_runs', 'articles_updated')
    op.drop_column('extraction_runs', 'articles_unchanged')
    op.drop_column('articles', 'content_hash')
