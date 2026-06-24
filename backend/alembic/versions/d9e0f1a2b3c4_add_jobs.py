"""add scheduled jobs (Job + JobRun); migrate per-source schedules into jobs

Introduces the "scheduled job" concept (like a backup job): a ``jobs`` row owns
a set of sources and one schedule, and fires into per-source extraction runs
grouped under a ``job_runs`` row. Scheduling moves off ``schedules`` (per source)
onto ``jobs``.

Backfill is non-destructive: every existing ``schedules`` row becomes a
single-source job carrying that source's schedule, and the source is assigned to
it. Sources without a schedule are left unassigned (``job_id`` NULL). The
``schedules`` table is dropped afterwards.

Revision ID: d9e0f1a2b3c4
Revises: c8d9e0f1a2b3
"""
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ENUM, UUID

revision: str = "d9e0f1a2b3c4"
down_revision: Union[str, Sequence[str], None] = "c8d9e0f1a2b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. jobs table.
    op.create_table(
        "jobs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(512), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("frequency", sa.String(16), nullable=True),
        sa.Column("time_of_day", sa.String(5), nullable=True),
        sa.Column("day_of_week", sa.Integer(), nullable=True),
        sa.Column("day_of_month", sa.Integer(), nullable=True),
        sa.Column("cron", sa.String(128), nullable=True),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="UTC"),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # 2. job_runs table.
    # create_type=False so create_table() below does NOT also emit CREATE TYPE
    # (which would be a second, un-guarded creation and fail). We create the type
    # once here with checkfirst=True, which is idempotent across retries.
    job_run_status = ENUM(
        "PENDING", "RUNNING", "COMPLETED", "PARTIAL", "FAILED", "CANCELLED",
        name="jobrunstatus", create_type=False,
    )
    job_run_status.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "job_runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "job_id", UUID(as_uuid=True),
            sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("status", job_run_status, nullable=False, server_default="PENDING"),
        sa.Column("trigger", sa.String(16), nullable=False, server_default="scheduled"),
        sa.Column("sources_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sources_done", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sources_failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_job_runs_job_id", "job_runs", ["job_id"])

    # 3. job_id on sources, job_run_id on extraction_runs.
    op.add_column(
        "documentation_sources",
        sa.Column(
            "job_id", UUID(as_uuid=True),
            sa.ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True,
        ),
    )
    op.create_index("ix_documentation_sources_job_id", "documentation_sources", ["job_id"])
    op.add_column(
        "extraction_runs",
        sa.Column(
            "job_run_id", UUID(as_uuid=True),
            sa.ForeignKey("job_runs.id", ondelete="SET NULL"), nullable=True,
        ),
    )
    op.create_index("ix_extraction_runs_job_run_id", "extraction_runs", ["job_run_id"])

    # 4. Backfill: one job per existing schedule, source assigned to it.
    #    UUIDs passed as text, cast with CAST(:x AS uuid) — never the ``:x::uuid``
    #    shorthand, which SQLAlchemy's text() leaves literal (see product layer).
    conn = op.get_bind()
    schedules = conn.execute(
        sa.text(
            "SELECT s.source_id::text AS sid, s.enabled, s.frequency, s.time_of_day, "
            "s.day_of_week, s.day_of_month, s.cron, s.timezone, s.next_run_at, "
            "s.last_run_at, src.name AS source_name "
            "FROM schedules s JOIN documentation_sources src ON src.id = s.source_id"
        )
    ).fetchall()
    ins_job = sa.text(
        "INSERT INTO jobs (id, name, enabled, frequency, time_of_day, day_of_week, "
        "day_of_month, cron, timezone, next_run_at, last_run_at, created_at, updated_at) "
        "VALUES (CAST(:id AS uuid), :name, :enabled, :frequency, :time_of_day, "
        ":day_of_week, :day_of_month, :cron, :timezone, :next_run_at, :last_run_at, "
        "now(), now())"
    )
    assign = sa.text(
        "UPDATE documentation_sources SET job_id = CAST(:jid AS uuid) "
        "WHERE id = CAST(:sid AS uuid)"
    )
    for s in schedules:
        jid = str(uuid.uuid4())
        conn.execute(ins_job, {
            "id": jid, "name": s.source_name, "enabled": s.enabled,
            "frequency": s.frequency, "time_of_day": s.time_of_day,
            "day_of_week": s.day_of_week, "day_of_month": s.day_of_month,
            "cron": s.cron, "timezone": s.timezone,
            "next_run_at": s.next_run_at, "last_run_at": s.last_run_at,
        })
        conn.execute(assign, {"jid": jid, "sid": s.sid})

    # 5. Drop the now-migrated per-source schedules table.
    op.drop_table("schedules")


def downgrade() -> None:
    # Recreate schedules (structure only) and backfill from single-source jobs.
    op.create_table(
        "schedules",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "source_id", UUID(as_uuid=True),
            sa.ForeignKey("documentation_sources.id", ondelete="CASCADE"),
            nullable=False, unique=True,
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("frequency", sa.String(16), nullable=False),
        sa.Column("time_of_day", sa.String(5), nullable=False, server_default="02:00"),
        sa.Column("day_of_week", sa.Integer(), nullable=True),
        sa.Column("day_of_month", sa.Integer(), nullable=True),
        sa.Column("cron", sa.String(128), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="UTC"),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_id", UUID(as_uuid=True),
                  sa.ForeignKey("extraction_runs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.execute(
        "INSERT INTO schedules (id, source_id, enabled, frequency, time_of_day, "
        "day_of_week, day_of_month, cron, timezone, next_run_at, last_run_at, "
        "created_at, updated_at) "
        "SELECT gen_random_uuid(), src.id, j.enabled, "
        "coalesce(j.frequency, 'daily'), coalesce(j.time_of_day, '02:00'), "
        "j.day_of_week, j.day_of_month, coalesce(j.cron, '0 2 * * *'), j.timezone, "
        "j.next_run_at, j.last_run_at, now(), now() "
        "FROM documentation_sources src JOIN jobs j ON j.id = src.job_id"
    )

    op.drop_index("ix_extraction_runs_job_run_id", table_name="extraction_runs")
    op.drop_column("extraction_runs", "job_run_id")
    op.drop_index("ix_documentation_sources_job_id", table_name="documentation_sources")
    op.drop_column("documentation_sources", "job_id")
    op.drop_index("ix_job_runs_job_id", table_name="job_runs")
    op.drop_table("job_runs")
    sa.Enum(name="jobrunstatus").drop(op.get_bind(), checkfirst=True)
    op.drop_table("jobs")
