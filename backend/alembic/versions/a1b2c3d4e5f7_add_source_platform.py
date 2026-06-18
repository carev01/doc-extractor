"""add source platform + profile_config

Revision ID: a1b2c3d4e5f7
Revises: f5a6b7c8d9e0
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "a1b2c3d4e5f7"
down_revision: Union[str, Sequence[str], None] = "f5a6b7c8d9e0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("documentation_sources", sa.Column("platform", sa.String(64), nullable=True))
    op.add_column("documentation_sources", sa.Column("profile_config", JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("documentation_sources", "profile_config")
    op.drop_column("documentation_sources", "platform")
