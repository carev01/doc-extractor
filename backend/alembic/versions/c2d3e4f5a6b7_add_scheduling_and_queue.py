"""add scheduling and queue

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "c2d3e4f5a6b7"
down_revision: Union[str, Sequence[str], None] = "b1c2d3e4f5a6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # New enum labels must be committed before they can be referenced.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE runstatus ADD VALUE IF NOT EXISTS 'PENDING'")
        op.execute("ALTER TYPE runstatus ADD VALUE IF NOT EXISTS 'CANCELLED'")

    op.add_column("extraction_runs", sa.Column("trigger", sa.String(16), server_default="manual", nullable=False))
    op.add_column("extraction_runs", sa.Column("claimed_by", sa.String(255), nullable=True))
    op.add_column("extraction_runs", sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("extraction_runs", sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("extraction_runs", sa.Column("attempts", sa.Integer(), server_default="0", nullable=False))

    op.create_index(
        "ix_runs_pending", "extraction_runs", ["created_at"],
        postgresql_where=sa.text("status = 'PENDING'"),
    )
    op.create_index(
        "uq_active_run_per_source", "extraction_runs", ["source_id"], unique=True,
        postgresql_where=sa.text("status IN ('PENDING', 'RUNNING')"),
    )

    op.create_table(
        "schedules",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("source_id", UUID(as_uuid=True), sa.ForeignKey("documentation_sources.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("frequency", sa.String(16), nullable=False),
        sa.Column("time_of_day", sa.String(5), nullable=False, server_default="02:00"),
        sa.Column("day_of_week", sa.Integer(), nullable=True),
        sa.Column("day_of_month", sa.Integer(), nullable=True),
        sa.Column("cron", sa.String(128), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="UTC"),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_id", UUID(as_uuid=True), sa.ForeignKey("extraction_runs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("schedules")
    op.drop_index("uq_active_run_per_source", table_name="extraction_runs")
    op.drop_index("ix_runs_pending", table_name="extraction_runs")
    op.drop_column("extraction_runs", "attempts")
    op.drop_column("extraction_runs", "heartbeat_at")
    op.drop_column("extraction_runs", "claimed_at")
    op.drop_column("extraction_runs", "claimed_by")
    op.drop_column("extraction_runs", "trigger")
    # Enum labels are left in place (Postgres cannot drop enum values cleanly).
