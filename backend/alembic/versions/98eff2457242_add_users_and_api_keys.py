"""Add users and api_keys tables for API authentication.

Revision ID: 98eff2457242
Revises: g1a2b3c4d5e6
Create Date: 2026-07-09 23:59:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "98eff2457242"
# Chains onto the current head (webhooks). NOTE: the userrole PG enum labels are
# the lowercase role *values* ("admin"/"read_write"/"read_only"); the ORM models
# match this via ``values_callable`` on their Enum columns (see models/user.py).
down_revision: Union[str, None] = "g1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create the user_role enum type once, explicitly. The column type below uses
    # create_type=False so op.create_table does NOT try to emit CREATE TYPE again
    # (which would fail with "type userrole already exists").
    sa.Enum("admin", "read_write", "read_only", name="userrole").create(
        op.get_bind(), checkfirst=True
    )
    user_role = postgresql.ENUM(
        "admin", "read_write", "read_only", name="userrole", create_type=False
    )

    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False, unique=True),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("hashed_password", sa.String(255), nullable=True),
        sa.Column("role", user_role, nullable=False, server_default="read_only"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("oauth_provider", sa.String(32), nullable=True),
        sa.Column("oauth_subject", sa.String(512), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    op.create_table(
        "api_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("key_prefix", sa.String(16), nullable=False),
        sa.Column("hashed_key", sa.String(128), nullable=False, unique=True),
        sa.Column("role", user_role, nullable=False, server_default="read_only"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_api_keys_user_id", "api_keys", ["user_id"])
    op.create_index("ix_api_keys_hashed_key", "api_keys", ["hashed_key"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_api_keys_hashed_key", table_name="api_keys")
    op.drop_index("ix_api_keys_user_id", table_name="api_keys")
    op.drop_table("api_keys")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")
    sa.Enum(name="userrole").drop(op.get_bind(), checkfirst=True)