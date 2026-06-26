"""auth_realms and source.auth_realm_id

Revision ID: 17c13db3546c
Revises: b2c3d4e5f6a7
Create Date: 2026-06-26 19:16:35.297272

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '17c13db3546c'
down_revision: Union[str, Sequence[str], None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'auth_realms',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('login_domain', sa.String(length=512), nullable=False),
        sa.Column('auth_type', sa.String(length=32), nullable=False),
        sa.Column('login_url', sa.String(length=2048), nullable=True),
        sa.Column('login_selectors', sa.Text(), nullable=True),
        sa.Column('username', sa.Text(), nullable=True),
        sa.Column('password', sa.Text(), nullable=True),
        sa.Column('totp_secret', sa.Text(), nullable=True),
        sa.Column('browserless_profile_name', sa.String(length=255), nullable=False),
        sa.Column('state_snapshot', sa.Text(), nullable=True),
        sa.Column('status', sa.Enum('ACTIVE', 'NEEDS_LOGIN', 'EXPIRED', 'LOGIN_FAILED', name='realmstatus'), nullable=False),
        sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('error_message', sa.String(length=4096), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.add_column('documentation_sources', sa.Column('auth_realm_id', sa.UUID(), nullable=True))
    op.create_foreign_key(
        'fk_documentation_sources_auth_realm_id',
        'documentation_sources', 'auth_realms',
        ['auth_realm_id'], ['id'],
        ondelete='SET NULL',
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('fk_documentation_sources_auth_realm_id', 'documentation_sources', type_='foreignkey')
    op.drop_column('documentation_sources', 'auth_realm_id')
    op.drop_table('auth_realms')
    op.execute("DROP TYPE IF EXISTS realmstatus")
