"""add export_jobs

Revision ID: f5a6b7c8d9e0
Revises: e4f5a6b7c8d9
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "f5a6b7c8d9e0"
down_revision: Union[str, Sequence[str], None] = "e4f5a6b7c8d9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "export_jobs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("source_id", UUID(as_uuid=True), sa.ForeignKey("documentation_sources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("request", JSONB, nullable=False),
        sa.Column("status", sa.Enum("PENDING", "RUNNING", "COMPLETED", "FAILED", "CANCELLED", name="exportstatus"), nullable=False),
        sa.Column("claimed_by", sa.String(255), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("export_id", UUID(as_uuid=True), nullable=True),
        sa.Column("result", JSONB, nullable=True),
        sa.Column("error_message", sa.String(4096), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_export_jobs_pending", "export_jobs", ["created_at"], postgresql_where=sa.text("status = 'PENDING'"))


def downgrade() -> None:
    op.drop_index("ix_export_jobs_pending", table_name="export_jobs")
    op.drop_table("export_jobs")
    sa.Enum(name="exportstatus").drop(op.get_bind(), checkfirst=True)
