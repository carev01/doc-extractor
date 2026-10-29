"""jobs overhaul: tiers, job targets, new schedule fields

Adds ``tiers`` (+ ``vendors.tier_id``), ``job_targets`` (a job's explicit
vendor/product/source coverage), the richer schedule columns on ``jobs``,
``job_runs.skipped`` and ``extraction_runs.auto_relogin_used``. Drops
``documentation_sources.job_id`` (existing assignments become source targets)
and ``jobs.frequency`` / ``jobs.day_of_week`` (mapped onto the new columns;
an hourly job has no equivalent, so it is disabled and loses its schedule).

Revision ID: e1f2a3b4c5d6
Revises: a7b8c9d0e1f2
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "e1f2a3b4c5d6"
down_revision = "a7b8c9d0e1f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tiers",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False, unique=True),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
    )
    op.add_column("vendors", sa.Column(
        "tier_id", UUID(as_uuid=True), sa.ForeignKey("tiers.id", ondelete="RESTRICT"), nullable=True))
    op.create_index("ix_vendors_tier_id", "vendors", ["tier_id"])

    op.add_column("jobs", sa.Column("schedule_kind", sa.String(16), nullable=True))
    op.add_column("jobs", sa.Column("interval_months", sa.Integer, nullable=True))
    op.add_column("jobs", sa.Column("start_month", sa.Integer, nullable=True))
    op.add_column("jobs", sa.Column("nth", sa.String(8), nullable=True))
    op.add_column("jobs", sa.Column("weekday", sa.Integer, nullable=True))
    op.add_column("jobs", sa.Column(
        "tier_id", UUID(as_uuid=True), sa.ForeignKey("tiers.id", ondelete="RESTRICT"), nullable=True))
    op.create_index("ix_jobs_tier_id", "jobs", ["tier_id"])

    # Carry existing schedules over before the old columns go.
    op.execute("""
        UPDATE jobs SET
          schedule_kind = CASE frequency
              WHEN 'daily' THEN 'daily' WHEN 'weekly' THEN 'weekly'
              WHEN 'monthly' THEN 'monthly_day' END,
          weekday = CASE WHEN frequency = 'weekly' THEN COALESCE(day_of_week, 0) END,
          interval_months = CASE WHEN frequency = 'monthly' THEN 1 END,
          day_of_month = CASE WHEN frequency = 'monthly' THEN COALESCE(day_of_month, 1)
                              ELSE day_of_month END
    """)
    # Hourly has no equivalent: stop scheduling it rather than keep a cron
    # the new rules cannot represent.
    op.execute("""
        UPDATE jobs SET enabled = false, cron = NULL, next_run_at = NULL
        WHERE frequency = 'hourly'
    """)

    op.create_table(
        "job_targets",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("job_id", UUID(as_uuid=True), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("vendor_id", UUID(as_uuid=True), sa.ForeignKey("vendors.id", ondelete="CASCADE"), nullable=True),
        sa.Column("product_id", UUID(as_uuid=True), sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=True),
        sa.Column("source_id", UUID(as_uuid=True),
                  sa.ForeignKey("documentation_sources.id", ondelete="CASCADE"), nullable=True),
        sa.CheckConstraint("num_nonnulls(vendor_id, product_id, source_id) = 1",
                           name="ck_job_targets_exactly_one_ref"),
        sa.UniqueConstraint("job_id", "vendor_id", name="uq_job_targets_job_vendor"),
        sa.UniqueConstraint("job_id", "product_id", name="uq_job_targets_job_product"),
        sa.UniqueConstraint("job_id", "source_id", name="uq_job_targets_job_source"),
    )
    for col in ("job_id", "vendor_id", "product_id", "source_id"):
        op.create_index(f"ix_job_targets_{col}", "job_targets", [col])

    # Existing source -> job assignments become explicit source targets.
    op.execute("""
        INSERT INTO job_targets (id, job_id, source_id)
        SELECT gen_random_uuid(), job_id, id FROM documentation_sources WHERE job_id IS NOT NULL
    """)

    op.drop_constraint("documentation_sources_job_id_fkey", "documentation_sources", type_="foreignkey")
    op.drop_column("documentation_sources", "job_id")
    op.drop_column("jobs", "frequency")
    op.drop_column("jobs", "day_of_week")

    op.add_column("job_runs", sa.Column("skipped", JSONB(none_as_null=True), nullable=True))
    op.add_column("extraction_runs", sa.Column(
        "auto_relogin_used", sa.Boolean, nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("extraction_runs", "auto_relogin_used")
    op.drop_column("job_runs", "skipped")

    op.add_column("jobs", sa.Column("frequency", sa.String(16), nullable=True))
    op.add_column("jobs", sa.Column("day_of_week", sa.Integer, nullable=True))
    op.execute("""
        UPDATE jobs SET
          frequency = CASE schedule_kind
              WHEN 'daily' THEN 'daily' WHEN 'weekly' THEN 'weekly'
              WHEN 'monthly_day' THEN 'monthly' END,
          day_of_week = CASE WHEN schedule_kind = 'weekly' THEN weekday END
    """)
    # monthly_nth / multi-month rules have no old equivalent: unschedule them.
    op.execute("""
        UPDATE jobs SET enabled = false, cron = NULL, next_run_at = NULL
        WHERE schedule_kind = 'monthly_nth'
           OR (schedule_kind = 'monthly_day' AND COALESCE(interval_months, 1) <> 1)
    """)

    op.add_column("documentation_sources", sa.Column("job_id", UUID(as_uuid=True), nullable=True))
    op.create_foreign_key("documentation_sources_job_id_fkey", "documentation_sources",
                          "jobs", ["job_id"], ["id"], ondelete="SET NULL")
    op.create_index("ix_documentation_sources_job_id", "documentation_sources", ["job_id"])
    # Restore a source's job when exactly one explicit source target names it.
    op.execute("""
        UPDATE documentation_sources s SET job_id = t.job_id
        FROM (SELECT source_id, min(job_id::text)::uuid AS job_id
              FROM job_targets WHERE source_id IS NOT NULL
              GROUP BY source_id HAVING count(*) = 1) t
        WHERE s.id = t.source_id
    """)

    op.drop_table("job_targets")
    op.drop_index("ix_jobs_tier_id", table_name="jobs")
    for col in ("tier_id", "weekday", "nth", "start_month", "interval_months", "schedule_kind"):
        op.drop_column("jobs", col)
    op.drop_index("ix_vendors_tier_id", table_name="vendors")
    op.drop_column("vendors", "tier_id")
    op.drop_table("tiers")
